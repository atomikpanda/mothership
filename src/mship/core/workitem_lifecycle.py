"""WorkItem lifecycle helpers for automated phase transitions.

Sibling of spec_lifecycle.py. `advance_spec_on_close` advances a spec bound to a
task via `task.spec_id` (the `mship spec dispatch` path). This advances a
WorkItem's completion state on the close of its last task — covering both the
spec-bound case a task-level spec_id misses (features spawned via
`mship spawn --work-item`, whose spec lives on the WorkItem) and the spec-less
case a spec can't cover at all.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def advance_workitem_on_close(
    *,
    task,
    workitems_dir: Path,
    specs_dir: Path,
    state,
    merged_count: int,
    closed_count: int,
) -> None:
    """Advance a WorkItem's completion state when its LAST live task closes after a clean merge.

    Two cases, both gated on this being the WorkItem's last live task + a clean
    full merge:

    - **Spec-bound WorkItem:** advance its approved/dispatched spec to
      `implemented`; compute_phase then projects a terminal spec status to
      `done`. This covers features spawned via `mship spawn --work-item`, whose
      spec stays `approved` on the WorkItem — `task.spec_id` is null, so
      `advance_spec_on_close`'s task-bound path never fires and the item would
      otherwise sit at `ready` forever.
    - **Spec-less WorkItem** (bug/chore/question): stamp `phase_override=done`,
      since compute_phase can't derive `done` without a terminal spec — otherwise
      the item falls to `inbox` and its merge conversation dead-ends on the
      now-removed task.

    Safe no-op if: `task.work_item_id` is None; not a clean full merge
    (`merged_count == 0` or `closed_count > 0`); the WorkItem is missing or
    already has a `phase_override`; another live task still references the
    WorkItem; or (spec-bound) the spec is missing or not in an advanceable
    status (`approved`/`dispatched`).

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

    this_slug = getattr(task, "slug", None)
    has_other_live_task = any(
        slug != this_slug and getattr(t, "work_item_id", None) == wid
        for slug, t in state.tasks.items()
    )
    if has_other_live_task:
        return

    if item.spec_id is not None:
        # Spec-bound: advance the WorkItem's spec so compute_phase derives `done`.
        from mship.core.spec_store import SpecStore

        sstore = SpecStore(specs_dir)
        spec = sstore.find_by_id(item.spec_id)
        if spec is not None and spec.status in ("approved", "dispatched"):
            now = datetime.now(timezone.utc)
            spec.status = "implemented"
            spec.updated_at = now
            sstore.save(spec)
            # Bubble the freshly-done WorkItem to the top of list()'s updated_at-desc
            # view too (mirrors the spec-less phase_override bump below). The override
            # stays None — the spec drives `done`; this only refreshes updated_at.
            store.set_phase_override(wid, None, now=now)
        return

    # Spec-less: stamp done directly. The updated_at bump (mirrors
    # advance_spec_on_close) keeps the freshly-`done` item at the top of
    # WorkItemStore.list()'s updated_at-desc view rather than buried.
    store.set_phase_override(wid, "done", now=datetime.now(timezone.utc))
