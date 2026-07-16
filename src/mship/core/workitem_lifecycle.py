"""WorkItem lifecycle helpers for automated phase transitions.

Sibling of spec_lifecycle.py: where advance_spec_on_close advances a bound
spec's status on merge-close (which compute_phase then projects to `done`),
this advances the WorkItem itself for the case a spec can't cover.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def advance_workitem_on_close(
    *,
    task,
    workitems_dir: Path,
    state,
    merged_count: int,
    closed_count: int,
) -> None:
    """Mark a spec-less WorkItem `done` when its LAST live task closes after a clean merge.

    A feature WorkItem reaches `done` through its spec: advance_spec_on_close
    moves the spec `dispatched → implemented`, and compute_phase projects a
    terminal spec status to `done` before it looks at task state. A spec-less
    WorkItem (bug/chore/question) has no such signal — once its task is closed
    it is removed from live state, leaving the item with no spec and no live
    tasks, so compute_phase falls through to `inbox`. Ground Control's item
    redirect then routes the item's conversation to its now-removed task
    ("this task is no longer available"). Stamping phase_override=done keeps the
    merge conversation grouped under a `done` WorkItem instead.

    Safe no-op if:
    - task.work_item_id is None
    - not a clean full merge (merged_count == 0 or closed_count > 0)
    - the WorkItem is missing, already has a phase_override, or has a bound spec
      (spec'd items are handled by advance_spec_on_close + compute_phase)
    - another live task still references this WorkItem (this isn't the last one)

    `state` is the pre-teardown State snapshot: it still contains the closing
    task, so the "last live task" check excludes it explicitly by slug.
    """
    wid = getattr(task, "work_item_id", None)
    if not wid:
        return
    if merged_count == 0 or closed_count > 0:
        return

    from mship.core.workitem_store import WorkItemStore

    store = WorkItemStore(workitems_dir)
    item = store.get(wid)
    if item is None:
        return
    if item.phase_override is not None:
        return
    if item.spec_id is not None:
        return

    this_slug = getattr(task, "slug", None)
    has_other_live_task = any(
        slug != this_slug and getattr(t, "work_item_id", None) == wid
        for slug, t in state.tasks.items()
    )
    if has_other_live_task:
        return

    # Stamp updated_at (mirrors advance_spec_on_close) so the freshly-`done` item
    # sorts to the top of WorkItemStore.list()'s updated_at-desc view rather than
    # staying buried at its stale timestamp.
    store.set_phase_override(wid, "done", now=datetime.now(timezone.utc))
