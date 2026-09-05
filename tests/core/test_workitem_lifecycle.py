"""Tests for advance_workitem_on_close (merge-close completion advancement).

Two behaviors, both gated on the closing task being the WorkItem's last live task
after a clean full merge:
- Spec-LESS WorkItem: stamp phase_override=done (compute_phase can't derive `done`
  without a terminal spec, so it would fall to `inbox` and its merge conversation
  would dead-end on the removed task).
- Spec-BOUND WorkItem: advance its approved/dispatched spec to `implemented`
  (compute_phase then projects a terminal spec → `done`). Covers features spawned
  via `mship spawn --work-item`, whose spec stays `approved` on the WorkItem.
"""
from datetime import datetime, timezone

import pytest

from mship.core import spec_key
from mship.core.spec_draft import new_spec
from mship.core.spec_store import SpecStore
from mship.core.spec_storage import SpecLocked, SpecStorage
from mship.core.state import Task, WorkspaceState
from mship.core.workitem_lifecycle import (
    TaskMetadataRetentionConflictError,
    advance_workitem_on_close,
    require_retained_task_metadata,
    retain_workitem_metadata_on_teardown,
)
from mship.core.workitem_store import WorkItemStore


def _now():
    return datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _dirs(tmp_path):
    return tmp_path / "workitems", tmp_path / "specs"


def _task(slug: str, work_item_id: str | None) -> Task:
    return Task(
        slug=slug,
        description=f"desc for {slug}",
        phase="dev",
        created_at=_now(),
        affected_repos=["r"],
        branch=f"feat/{slug}",
        worktrees={},
        work_item_id=work_item_id,
    )


def _store_with_item(tmp_path, *, kind="chore", spec_id=None):
    store = WorkItemStore(tmp_path / "workitems")
    wi = store.create(title="t", kind=kind, workspace="ws", now=_now())
    if spec_id is not None:
        store.link_spec(wi.id, spec_id, now=_now())
    return store, wi


def _make_spec(tmp_path, *, status: str) -> str:
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    sstore = SpecStore(specs_dir)
    spec = new_spec("Feature", now=_now(), task_slug="task-a")
    spec.status = status
    sstore.save(spec)
    return spec.id


def _call(tmp_path, task, state, *, merged_count=1, closed_count=0):
    workitems_dir, specs_dir = _dirs(tmp_path)
    advance_workitem_on_close(
        task=task, workitems_dir=workitems_dir, specs_dir=specs_dir, state=state,
        merged_count=merged_count, closed_count=closed_count,
    )


# --- spec-less path ------------------------------------------------------------

def test_marks_spec_less_workitem_done_on_last_task_merge(tmp_path):
    store, wi = _store_with_item(tmp_path)
    store.add_task(wi.id, "task-a", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    _call(tmp_path, t, state)

    got = store.get(wi.id)
    assert got.phase_override == "done"
    # updated_at must be refreshed so the freshly-done item isn't buried in the
    # updated_at-desc list() view (Greptile #362).
    assert got.updated_at != _now()


def test_no_op_when_other_live_task_remains(tmp_path):
    store, wi = _store_with_item(tmp_path)
    store.add_task(wi.id, "task-a", now=_now())
    store.add_task(wi.id, "task-b", now=_now())
    closing = _task("task-a", wi.id)
    sibling = _task("task-b", wi.id)
    state = WorkspaceState(tasks={"task-a": closing, "task-b": sibling})

    _call(tmp_path, closing, state)

    assert store.get(wi.id).phase_override is None


def test_no_op_when_phase_override_already_set(tmp_path):
    store, wi = _store_with_item(tmp_path)
    store.add_task(wi.id, "task-a", now=_now())
    store.set_phase_override(wi.id, "review", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    _call(tmp_path, t, state)

    assert store.get(wi.id).phase_override == "review"


def test_no_op_when_not_fully_merged(tmp_path):
    store, wi = _store_with_item(tmp_path)
    store.add_task(wi.id, "task-a", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    # Abandoned / cancelled-on-github / no merges: leave phase derived.
    _call(tmp_path, t, state, merged_count=0, closed_count=1)
    _call(tmp_path, t, state, merged_count=1, closed_count=1)

    assert store.get(wi.id).phase_override is None


def test_no_op_when_no_work_item_id(tmp_path):
    store, wi = _store_with_item(tmp_path)
    t = _task("task-a", None)
    state = WorkspaceState(tasks={"task-a": t})

    _call(tmp_path, t, state)

    assert store.get(wi.id).phase_override is None


def test_no_op_when_workitem_missing(tmp_path):
    # Dangling work_item_id (item deleted) must not raise.
    t = _task("task-a", "wi-does-not-exist")
    state = WorkspaceState(tasks={"task-a": t})

    _call(tmp_path, t, state)  # no exception


# --- spec-bound path -----------------------------------------------------------

def test_advances_approved_spec_on_last_task_merge(tmp_path):
    # The reported gap: spawn --work-item leaves the spec `approved` on the WorkItem
    # (task.spec_id null), so it must be advanced here to reach `done`.
    spec_id = _make_spec(tmp_path, status="approved")
    store, wi = _store_with_item(tmp_path, kind="feature", spec_id=spec_id)
    store.add_task(wi.id, "task-a", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    _call(tmp_path, t, state)

    assert SpecStore(tmp_path / "specs").find_by_id(spec_id).status == "implemented"
    got = store.get(wi.id)
    # Spec-bound items reach done via the spec, not a phase_override.
    assert got.phase_override is None
    # ...but the WorkItem's updated_at is still bumped so the freshly-done feature
    # bubbles to the top of list()'s updated_at-desc view (Greptile #365).
    assert got.updated_at != _now()


def test_advances_dispatched_spec(tmp_path):
    spec_id = _make_spec(tmp_path, status="dispatched")
    store, wi = _store_with_item(tmp_path, kind="feature", spec_id=spec_id)
    store.add_task(wi.id, "task-a", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    _call(tmp_path, t, state)

    assert SpecStore(tmp_path / "specs").find_by_id(spec_id).status == "implemented"


def test_spec_bound_no_advance_when_other_live_task(tmp_path):
    spec_id = _make_spec(tmp_path, status="approved")
    store, wi = _store_with_item(tmp_path, kind="feature", spec_id=spec_id)
    store.add_task(wi.id, "task-a", now=_now())
    store.add_task(wi.id, "task-b", now=_now())
    closing = _task("task-a", wi.id)
    sibling = _task("task-b", wi.id)
    state = WorkspaceState(tasks={"task-a": closing, "task-b": sibling})

    _call(tmp_path, closing, state)

    # A sibling task is still implementing the spec — don't advance prematurely.
    assert SpecStore(tmp_path / "specs").find_by_id(spec_id).status == "approved"


def test_no_advance_when_spec_not_approvable(tmp_path):
    # A spec that isn't approved/dispatched (e.g. still in review) must not be
    # force-advanced to implemented.
    spec_id = _make_spec(tmp_path, status="needs_review")
    store, wi = _store_with_item(tmp_path, kind="feature", spec_id=spec_id)
    store.add_task(wi.id, "task-a", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    _call(tmp_path, t, state)

    assert SpecStore(tmp_path / "specs").find_by_id(spec_id).status == "needs_review"


def test_spec_bound_missing_spec_no_raise(tmp_path):
    # WorkItem points at a spec that doesn't exist on disk: no crash, no override.
    store, wi = _store_with_item(tmp_path, kind="feature", spec_id="spec-does-not-exist")
    store.add_task(wi.id, "task-a", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    _call(tmp_path, t, state)  # no exception

    assert store.get(wi.id).phase_override is None

def test_spec_bound_close_raises_for_locked_spec(tmp_path):
    specs_dir = tmp_path / "specs"
    encrypted = SpecStore(
        specs_dir,
        storage=SpecStorage(specs_dir, mode="encrypted", workspace_root=tmp_path),
    )
    spec = new_spec("Feature", now=_now(), task_slug="task-a")
    spec.status = "approved"
    encrypted.save(spec)
    spec_key.keyfile_path(tmp_path).unlink()
    store, wi = _store_with_item(tmp_path, kind="feature", spec_id=spec.id)
    store.add_task(wi.id, "task-a", now=_now())
    task = _task("task-a", wi.id)

    with pytest.raises(SpecLocked):
        _call(tmp_path, task, WorkspaceState(tasks={"task-a": task}))


# --- push-only completion (completed_without_prs) ------------------------------

def test_completed_without_prs_marks_spec_less_workitem_done(tmp_path):
    """finish --push-only → close: no PRs (merged_count=0), but the caller vouches
    for completion — the spec-less item still stamps done."""
    store, wi = _store_with_item(tmp_path)
    store.add_task(wi.id, "task-a", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    workitems_dir, specs_dir = _dirs(tmp_path)
    advance_workitem_on_close(
        task=t, workitems_dir=workitems_dir, specs_dir=specs_dir, state=state,
        merged_count=0, closed_count=0, completed_without_prs=True,
    )

    assert store.get(wi.id).phase_override == "done"


def test_no_completion_flag_and_no_merge_stays_unadvanced(tmp_path):
    """Default (completed_without_prs=False) with no merged PRs remains a no-op."""
    store, wi = _store_with_item(tmp_path)
    store.add_task(wi.id, "task-a", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    _call(tmp_path, t, state, merged_count=0)

    assert store.get(wi.id).phase_override is None


# --- delivery metadata retention ----------------------------------------------

@pytest.mark.parametrize(
    ("reverse_item_id", "archived"),
    [
        pytest.param(None, False, id="forward-only"),
        pytest.param("wi-stale", True, id="stale-reverse-archived-forward"),
    ],
)
def test_teardown_retains_metadata_from_authoritative_forward_link(
    tmp_path, reverse_item_id, archived,
):
    store, item = _store_with_item(tmp_path)
    store.add_task(item.id, "task-a", now=_now())
    if archived:
        store.archive(item.id, now=_now())
    task = _task("task-a", reverse_item_id)
    task.affected_repos = ["api"]
    task.pr_urls = {"api": "https://github.example/api/pull/1"}

    retained = retain_workitem_metadata_on_teardown(
        task=task, workitems_dir=tmp_path / "workitems",
    )

    require_retained_task_metadata(task, retained)
    persisted = store.get(item.id)
    assert persisted.affected_repos == ["api"]
    assert persisted.pr_urls == ["https://github.example/api/pull/1"]


def test_teardown_refuses_ambiguous_forward_links(tmp_path):
    store, first = _store_with_item(tmp_path)
    second = store.create(title="other", kind="chore", workspace="ws", now=_now())
    store.add_task(first.id, "task-a", now=_now())
    store.add_task(second.id, "task-a", now=_now())
    task = _task("task-a", None)
    task.affected_repos = ["api"]

    with pytest.raises(TaskMetadataRetentionConflictError, match="ambiguous"):
        retain_workitem_metadata_on_teardown(
            task=task, workitems_dir=tmp_path / "workitems",
        )

    assert store.get(first.id).affected_repos == []
    assert store.get(second.id).affected_repos == []
