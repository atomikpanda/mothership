"""Tests for advance_workitem_on_close (merge-close phase advancement).

Regression coverage for the "merge notification dead-ends on a removed task"
bug: a spec-less WorkItem whose last task merged+closed used to fall to the
`inbox` phase (compute_phase can only derive `done` from a terminal spec), so
Ground Control routed its conversation to the now-removed task ("this task is
no longer available"). advance_workitem_on_close stamps phase_override=done so
the conversation stays grouped under a `done` WorkItem instead.
"""
from datetime import datetime, timezone

from mship.core.state import Task, WorkspaceState
from mship.core.workitem_lifecycle import advance_workitem_on_close
from mship.core.workitem_store import WorkItemStore


def _now():
    return datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


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


def test_marks_spec_less_workitem_done_on_last_task_merge(tmp_path):
    store, wi = _store_with_item(tmp_path)
    store.add_task(wi.id, "task-a", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    advance_workitem_on_close(
        task=t, workitems_dir=tmp_path / "workitems", state=state,
        merged_count=1, closed_count=0,
    )

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

    advance_workitem_on_close(
        task=closing, workitems_dir=tmp_path / "workitems", state=state,
        merged_count=1, closed_count=0,
    )

    assert store.get(wi.id).phase_override is None


def test_no_op_when_spec_bound(tmp_path):
    # Feature WorkItems reach `done` via advance_spec_on_close + compute_phase's
    # terminal-spec check; this helper must not clobber that path.
    store, wi = _store_with_item(tmp_path, kind="feature", spec_id="spec-1")
    store.add_task(wi.id, "task-a", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    advance_workitem_on_close(
        task=t, workitems_dir=tmp_path / "workitems", state=state,
        merged_count=1, closed_count=0,
    )

    assert store.get(wi.id).phase_override is None


def test_no_op_when_phase_override_already_set(tmp_path):
    store, wi = _store_with_item(tmp_path)
    store.add_task(wi.id, "task-a", now=_now())
    store.set_phase_override(wi.id, "review", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    advance_workitem_on_close(
        task=t, workitems_dir=tmp_path / "workitems", state=state,
        merged_count=1, closed_count=0,
    )

    assert store.get(wi.id).phase_override == "review"


def test_no_op_when_not_fully_merged(tmp_path):
    store, wi = _store_with_item(tmp_path)
    store.add_task(wi.id, "task-a", now=_now())
    t = _task("task-a", wi.id)
    state = WorkspaceState(tasks={"task-a": t})

    # Abandoned / cancelled-on-github / no merges: leave phase derived.
    advance_workitem_on_close(
        task=t, workitems_dir=tmp_path / "workitems", state=state,
        merged_count=0, closed_count=1,
    )
    advance_workitem_on_close(
        task=t, workitems_dir=tmp_path / "workitems", state=state,
        merged_count=1, closed_count=1,
    )

    assert store.get(wi.id).phase_override is None


def test_no_op_when_no_work_item_id(tmp_path):
    store, wi = _store_with_item(tmp_path)
    t = _task("task-a", None)
    state = WorkspaceState(tasks={"task-a": t})

    advance_workitem_on_close(
        task=t, workitems_dir=tmp_path / "workitems", state=state,
        merged_count=1, closed_count=0,
    )

    assert store.get(wi.id).phase_override is None


def test_no_op_when_workitem_missing(tmp_path):
    # Dangling work_item_id (item deleted) must not raise.
    t = _task("task-a", "wi-does-not-exist")
    state = WorkspaceState(tasks={"task-a": t})

    advance_workitem_on_close(
        task=t, workitems_dir=tmp_path / "workitems", state=state,
        merged_count=1, closed_count=0,
    )  # no exception
