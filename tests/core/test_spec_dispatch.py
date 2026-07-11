from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.spec import AcceptanceCriterion, Spec
from mship.core.spec_body import render_body
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.spec_dispatch import DispatchError, build_dispatch_handoff, dispatch_spec
from mship.core.workitem_store import WorkItemStore


NOW = datetime(2026, 6, 14, tzinfo=timezone.utc)
WORKSPACE = "testws"


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


def _items(tmp_path) -> WorkItemStore:
    return WorkItemStore(tmp_path / "workitems")


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
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={"dq": _task()}))
    spec = _approved_spec()
    store.save(spec)

    def spawn_fn(_s):
        raise AssertionError("must not auto-spawn when the task already exists")

    result = dispatch_spec(
        spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW,
        workitems=items, workspace=WORKSPACE,
    )

    assert result.spawned is False
    assert result.spec.status == "dispatched"
    assert result.spec.task_slug == "dq"
    assert store.find_by_id("dq").status == "dispatched"   # persisted
    assert sm.load().tasks["dq"].spec_id == "dq"           # task bound to spec


def test_dispatch_spec_auto_spawns_when_no_task(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={}))
    spec = _approved_spec()
    store.save(spec)

    calls = []

    def spawn_fn(s):
        calls.append((s.id, tuple(s.affected_repos)))
        task = _task(slug=s.id, repos=s.affected_repos)
        sm.mutate(lambda st: st.tasks.__setitem__(s.id, task))
        return task

    result = dispatch_spec(
        spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW,
        workitems=items, workspace=WORKSPACE,
    )

    assert result.spawned is True
    assert calls == [("dq", ("mothership",))]              # spawn_fn got the spec
    assert result.task.slug == "dq"
    assert sm.load().tasks["dq"].spec_id == "dq"
    assert store.find_by_id("dq").status == "dispatched"


def test_dispatch_spec_requires_approved(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    spec = _approved_spec(status="needs_review")
    store.save(spec)
    with pytest.raises(DispatchError):
        dispatch_spec(
            spec, state_manager=sm, store=store, spawn_fn=lambda s: None, now=NOW,
            workitems=items, workspace=WORKSPACE,
        )


def test_dispatch_spec_auto_spawn_requires_affected_repos(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={}))
    spec = _approved_spec(affected_repos=[])
    store.save(spec)
    with pytest.raises(DispatchError):
        dispatch_spec(
            spec, state_manager=sm, store=store, spawn_fn=lambda s: None, now=NOW,
            workitems=items, workspace=WORKSPACE,
        )


def test_dispatch_spec_binds_existing_differently_slugged_task(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={"other": _task(slug="other")}))
    spec = _approved_spec()  # id == "dq", no task named "dq"
    store.save(spec)

    def spawn_fn(_s):
        raise AssertionError("must not auto-spawn when --task binds an existing task")

    result = dispatch_spec(
        spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW, task_slug="other",
        workitems=items, workspace=WORKSPACE,
    )

    assert result.spawned is False
    assert result.task.slug == "other"
    assert result.spec.task_slug == "other"
    assert "dq" not in sm.load().tasks                      # no duplicate spawned
    assert sm.load().tasks["other"].spec_id == "dq"         # the existing task is bound


def test_dispatch_spec_unknown_task_errors(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={}))
    spec = _approved_spec()
    store.save(spec)
    with pytest.raises(DispatchError, match="--task"):
        dispatch_spec(
            spec, state_manager=sm, store=store,
            spawn_fn=lambda s: None, now=NOW, task_slug="ghost",
            workitems=items, workspace=WORKSPACE,
        )


def test_dispatch_spec_is_idempotent_when_already_bound(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={"other": _task(slug="other")}))
    spec = _approved_spec()
    store.save(spec)

    dispatch_spec(
        spec, state_manager=sm, store=store,
        spawn_fn=lambda s: (_ for _ in ()).throw(AssertionError("no spawn")),
        now=NOW, task_slug="other",
        workitems=items, workspace=WORKSPACE,
    )
    result = dispatch_spec(
        spec, state_manager=sm, store=store,
        spawn_fn=lambda s: (_ for _ in ()).throw(AssertionError("no spawn")),
        now=NOW,
        workitems=items, workspace=WORKSPACE,
    )
    assert result.spawned is False
    assert result.task.slug == "other"
    assert list(sm.load().tasks) == ["other"]               # still exactly one task


def test_dispatch_spec_rebind_conflict_errors(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={
        "other": _task(slug="other"),
        "another": _task(slug="another"),
    }))
    spec = _approved_spec()
    store.save(spec)
    dispatch_spec(
        spec, state_manager=sm, store=store,
        spawn_fn=lambda s: None, now=NOW, task_slug="other",
        workitems=items, workspace=WORKSPACE,
    )
    with pytest.raises(DispatchError, match="already bound"):
        dispatch_spec(
            spec, state_manager=sm, store=store,
            spawn_fn=lambda s: None, now=NOW, task_slug="another",
            workitems=items, workspace=WORKSPACE,
        )


# --- MOS-213: dispatch attaches a feature WorkItem to the spawned/bound task ---

def test_dispatch_spec_auto_spawn_creates_and_links_feature_workitem(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={}))
    spec = _approved_spec()
    store.save(spec)

    def spawn_fn(s):
        task = _task(slug=s.id, repos=s.affected_repos)
        sm.mutate(lambda st: st.tasks.__setitem__(s.id, task))
        return task

    result = dispatch_spec(
        spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW,
        workitems=items, workspace=WORKSPACE,
    )

    chosen_slug = result.task.slug
    assert sm.load().tasks[chosen_slug].work_item_id is not None   # reverse link on the task

    all_items = items.list()
    assert len(all_items) == 1
    wi = all_items[0]
    assert wi.kind == "feature"
    assert wi.workspace == WORKSPACE
    assert wi.spec_id == spec.id
    assert chosen_slug in wi.task_slugs

    assert result.spec.work_item_id == wi.id
    assert store.find_by_id(spec.id).work_item_id == wi.id         # persisted


def test_dispatch_spec_reuses_existing_workitem_when_spec_already_has_one(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={"dq": _task()}))
    existing = items.create(title="Decision Queue", kind="feature", workspace=WORKSPACE, now=NOW)
    items.link_spec(existing.id, "dq", now=NOW)
    spec = _approved_spec(work_item_id=existing.id)
    store.save(spec)

    def spawn_fn(_s):
        raise AssertionError("must not auto-spawn when the task already exists")

    result = dispatch_spec(
        spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW,
        workitems=items, workspace=WORKSPACE,
    )

    all_items = items.list()
    assert len(all_items) == 1                                     # no new item created
    wi = all_items[0]
    assert wi.id == existing.id
    assert "dq" in wi.task_slugs
    assert result.spec.work_item_id == existing.id
    assert sm.load().tasks["dq"].work_item_id == existing.id


# --- MOS-228 T4: WorkItem-join adopt + refuse-to-guess ---

def test_dispatch_spec_adopts_via_workitem_join(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={"other-task": _task(slug="other-task")}))
    wi = items.create(title="Decision Queue", kind="feature", workspace=WORKSPACE, now=NOW)
    items.add_task(wi.id, "other-task", now=NOW, state=sm)
    spec = _approved_spec(work_item_id=wi.id)  # id == "dq"; unbound; task slug != spec.id
    store.save(spec)

    def spawn_fn(_s):
        raise AssertionError("must not auto-spawn when the WorkItem join resolves a live task")

    result = dispatch_spec(
        spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW,
        workitems=items, workspace=WORKSPACE,
    )

    assert result.spawned is False
    assert result.task.slug == "other-task"
    assert result.spec.task_slug == "other-task"
    assert sm.load().tasks["other-task"].spec_id == "dq"
    assert [i.id for i in items.list()] == [wi.id]              # no new WorkItem minted


def test_dispatch_spec_refuses_to_guess_with_multiple_workitem_candidates(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={"t1": _task(slug="t1"), "t2": _task(slug="t2")}))
    wi = items.create(title="Decision Queue", kind="feature", workspace=WORKSPACE, now=NOW)
    items.add_task(wi.id, "t1", now=NOW, state=sm)
    items.add_task(wi.id, "t2", now=NOW, state=sm)
    spec = _approved_spec(work_item_id=wi.id)  # id == "dq"; unbound
    store.save(spec)

    def spawn_fn(_s):
        raise AssertionError("must not auto-spawn when the join is ambiguous")

    with pytest.raises(DispatchError, match="--task"):
        dispatch_spec(
            spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW,
            workitems=items, workspace=WORKSPACE,
        )

    assert store.find_by_id("dq").status == "approved"          # unchanged, not dispatched
    assert sorted(sm.load().tasks) == ["t1", "t2"]               # nothing spawned
    assert [i.id for i in items.list()] == [wi.id]               # no new WorkItem minted
    assert sorted(items.get(wi.id).task_slugs) == ["t1", "t2"]   # WorkItem untouched


def test_dispatch_spec_ignores_closed_workitem_candidates_and_auto_spawns(tmp_path):
    sm, store, items = _sm(tmp_path), _store(tmp_path), _items(tmp_path)
    sm.save(WorkspaceState(tasks={}))
    wi = items.create(title="Decision Queue", kind="feature", workspace=WORKSPACE, now=NOW)
    wi.task_slugs.append("closed-task")  # simulate a closed task: absent from state.tasks
    items.save(wi)
    spec = _approved_spec(work_item_id=wi.id)
    store.save(spec)

    calls = []

    def spawn_fn(s):
        calls.append(s.id)
        task = _task(slug=s.id, repos=s.affected_repos)
        sm.mutate(lambda st: st.tasks.__setitem__(s.id, task))
        return task

    result = dispatch_spec(
        spec, state_manager=sm, store=store, spawn_fn=spawn_fn, now=NOW,
        workitems=items, workspace=WORKSPACE,
    )

    assert result.spawned is True
    assert calls == ["dq"]
    assert result.task.slug == "dq"
    assert sorted(items.get(wi.id).task_slugs) == ["closed-task", "dq"]
