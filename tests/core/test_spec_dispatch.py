from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.spec import AcceptanceCriterion, Spec
from mship.core.spec_body import render_body
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.spec_dispatch import DispatchError, build_dispatch_handoff, dispatch_spec


NOW = datetime(2026, 6, 14, tzinfo=timezone.utc)


def _approved_spec(**over) -> Spec:
    data = dict(
        id="dq", title="Decision Queue", status="approved",
        created_at=NOW, updated_at=NOW, affected_repos=["mothership"],
        body=render_body("the problem", "as a user", "the approach"),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="view questions", verdict="approved")],
    )
    data.update(over)
    return Spec(**data)


def _store(tmp_path) -> SpecStore:
    return SpecStore(tmp_path / "specs")


def _sm(tmp_path) -> StateManager:
    d = tmp_path / ".mothership"
    d.mkdir(exist_ok=True)
    return StateManager(d)


def _task(slug="dq", repos=("mothership",)) -> Task:
    return Task(
        slug=slug, description="d", phase="plan", created_at=NOW,
        affected_repos=list(repos), branch=f"feat/{slug}",
        worktrees={r: Path(f"/wt/{slug}/{r}") for r in repos},
    )


def test_build_dispatch_handoff_contains_key_facts():
    out = build_dispatch_handoff(_approved_spec(), _task())
    assert "Decision Queue" in out                 # title
    assert "dq" in out                             # spec id / task slug
    assert "feat/dq" in out                        # branch
    assert "the problem" in out                    # Problem section from body
    assert "ac1" in out and "view questions" in out  # acceptance criteria
    assert "/wt/dq/mothership" in out              # worktree path
    assert "mship dispatch --task dq" in out       # next-step command


def test_dispatch_spec_binds_existing_task_without_spawning(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    sm.save(WorkspaceState(tasks={"dq": _task()}))
    spec = _approved_spec()
    store.save(spec)

    def spawn_fn(_s):
        raise AssertionError("must not auto-spawn when the task already exists")

    result = dispatch_spec(spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW)

    assert result.spawned is False
    assert result.spec.status == "dispatched"
    assert result.spec.task_slug == "dq"
    assert store.find_by_id("dq").status == "dispatched"   # persisted
    assert sm.load().tasks["dq"].spec_id == "dq"           # task bound to spec


def test_dispatch_spec_auto_spawns_when_no_task(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    sm.save(WorkspaceState(tasks={}))
    spec = _approved_spec()
    store.save(spec)

    calls = []

    def spawn_fn(s):
        calls.append((s.id, tuple(s.affected_repos)))
        task = _task(slug=s.id, repos=s.affected_repos)
        sm.mutate(lambda st: st.tasks.__setitem__(s.id, task))
        return task

    result = dispatch_spec(spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW)

    assert result.spawned is True
    assert calls == [("dq", ("mothership",))]              # spawn_fn got the spec
    assert result.task.slug == "dq"
    assert sm.load().tasks["dq"].spec_id == "dq"
    assert store.find_by_id("dq").status == "dispatched"


def test_dispatch_spec_requires_approved(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    spec = _approved_spec(status="needs_review")
    store.save(spec)
    with pytest.raises(DispatchError):
        dispatch_spec(spec, state_manager=sm, store=store, spawn_fn=lambda s: None, now=NOW)


def test_dispatch_spec_auto_spawn_requires_affected_repos(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    sm.save(WorkspaceState(tasks={}))
    spec = _approved_spec(affected_repos=[])
    store.save(spec)
    with pytest.raises(DispatchError):
        dispatch_spec(spec, state_manager=sm, store=store, spawn_fn=lambda s: None, now=NOW)


def test_dispatch_spec_binds_existing_differently_slugged_task(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    sm.save(WorkspaceState(tasks={"other": _task(slug="other")}))
    spec = _approved_spec()  # id == "dq", no task named "dq"
    store.save(spec)

    def spawn_fn(_s):
        raise AssertionError("must not auto-spawn when --task binds an existing task")

    result = dispatch_spec(
        spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW, task_slug="other"
    )

    assert result.spawned is False
    assert result.task.slug == "other"
    assert result.spec.task_slug == "other"
    assert "dq" not in sm.load().tasks                      # no duplicate spawned
    assert sm.load().tasks["other"].spec_id == "dq"         # the existing task is bound


def test_dispatch_spec_unknown_task_errors(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    sm.save(WorkspaceState(tasks={}))
    spec = _approved_spec()
    store.save(spec)
    with pytest.raises(DispatchError, match="--task"):
        dispatch_spec(
            spec, state_manager=sm, store=store,
            spawn_fn=lambda s: None, now=NOW, task_slug="ghost",
        )


def test_dispatch_spec_is_idempotent_when_already_bound(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    sm.save(WorkspaceState(tasks={"other": _task(slug="other")}))
    spec = _approved_spec()
    store.save(spec)

    dispatch_spec(
        spec, state_manager=sm, store=store,
        spawn_fn=lambda s: (_ for _ in ()).throw(AssertionError("no spawn")),
        now=NOW, task_slug="other",
    )
    result = dispatch_spec(
        spec, state_manager=sm, store=store,
        spawn_fn=lambda s: (_ for _ in ()).throw(AssertionError("no spawn")),
        now=NOW,
    )
    assert result.spawned is False
    assert result.task.slug == "other"
    assert list(sm.load().tasks) == ["other"]               # still exactly one task


def test_dispatch_spec_rebind_conflict_errors(tmp_path):
    sm, store = _sm(tmp_path), _store(tmp_path)
    sm.save(WorkspaceState(tasks={
        "other": _task(slug="other"),
        "another": _task(slug="another"),
    }))
    spec = _approved_spec()
    store.save(spec)
    dispatch_spec(
        spec, state_manager=sm, store=store,
        spawn_fn=lambda s: None, now=NOW, task_slug="other",
    )
    with pytest.raises(DispatchError, match="already bound"):
        dispatch_spec(
            spec, state_manager=sm, store=store,
            spawn_fn=lambda s: None, now=NOW, task_slug="another",
        )
