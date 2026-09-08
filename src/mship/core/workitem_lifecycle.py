"""WorkItem lifecycle helpers for automated phase transitions.

Sibling of spec_lifecycle.py. `advance_spec_on_close` advances a spec bound to a
task via `task.spec_id` (the `mship spec dispatch` path). This advances a
WorkItem's completion state on the close of its last task — covering both the
spec-bound case a task-level spec_id misses (features spawned via
`mship spawn --work-item`, whose spec lives on the WorkItem) and the spec-less
case a spec can't cover at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mship.core.workitem_store import WorkItemStore


@dataclass(frozen=True)
class RetainedTaskMetadata:
    """Delivery metadata persisted before transient task teardown."""

    work_item_id: str | None
    affected_repos: frozenset[str]
    pr_urls: frozenset[str]


class TaskMetadataRetentionConflictError(RuntimeError):
    """Raised when task delivery metadata changes after retention."""

    def __init__(self, task_slug: str, reason: str) -> None:
        super().__init__(
            f"Refusing to remove task '{task_slug}': {reason}. "
            "Retry the operation so the latest delivery metadata is retained."
        )


def _task_metadata(task) -> RetainedTaskMetadata:
    return RetainedTaskMetadata(
        work_item_id=getattr(task, "work_item_id", None),
        affected_repos=frozenset(getattr(task, "affected_repos", []) or []),
        pr_urls=frozenset((getattr(task, "pr_urls", {}) or {}).values()),
    )


def retain_workitem_metadata_on_teardown(*, task, workitems_dir: Path) -> RetainedTaskMetadata:
    """Persist a task's delivery metadata before it leaves transient state.

    A WorkItem's forward ``task_slugs`` link is durable enough to guard archive,
    so it is authoritative when the task-side link is absent or stale. Resolve
    that relationship before taking an item lock; state mutation callers invoke
    this helper before acquiring state.lock.
    """
    retained = _task_metadata(task)

    from mship.core.workitem_store import TaskLinkAmbiguousError, WorkItemStore

    store = WorkItemStore(workitems_dir)
    try:
        item_id = store.resolve_task_workitem_id(task.slug, retained.work_item_id)
    except TaskLinkAmbiguousError as exc:
        raise TaskMetadataRetentionConflictError(
            task.slug, f"forward WorkItem link is ambiguous ({', '.join(exc.item_ids)})",
        ) from exc

    if not item_id:
        return retained

    try:
        retained_successfully = store.retain_task_metadata(task, item_id=item_id)
    except ValueError as exc:
        raise TaskMetadataRetentionConflictError(
            task.slug, f"linked work item {item_id!r} is unavailable",
        ) from exc
    if not retained_successfully:
        raise TaskMetadataRetentionConflictError(
            task.slug, f"linked work item {item_id!r} is unavailable",
        )
    return retained


def require_retained_task_metadata(
    task, retained: RetainedTaskMetadata | None,
) -> None:
    """Refuse teardown unless the live task's delivery metadata was retained."""
    if retained is None:
        raise TaskMetadataRetentionConflictError(
            task.slug, "no durable metadata snapshot was recorded",
        )

    current = _task_metadata(task)
    if (
        current.work_item_id != retained.work_item_id
        or not current.affected_repos.issubset(retained.affected_repos)
        or not current.pr_urls.issubset(retained.pr_urls)
    ):
        raise TaskMetadataRetentionConflictError(
            task.slug,
            "its delivery metadata changed after it was retained",
        )


def advance_workitem_on_close(
    *,
    task,
    workitems_dir: Path | None = None,
    workitems: WorkItemStore | None = None,
    specs_dir: Path,
    state,
    merged_count: int,
    closed_count: int,
    completed_without_prs: bool = False,
) -> None:
    """Advance a WorkItem's completion state when its LAST live task closes after a clean merge.

    `completed_without_prs` mirrors advance_spec_on_close: the `finish
    --push-only` → local merge → `close` route never has PRs, so the caller
    asserts completion explicitly rather than faking a merge count. Only passed
    for a finished, pushed, non-abandoned, non-forced close.

    Two cases, both gated on this being the WorkItem's last live task + a
    delivered close (clean full merge, or completed_without_prs):

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
    if not completed_without_prs and (merged_count == 0 or closed_count > 0):
        return

    if workitems is not None:
        store = workitems
    elif workitems_dir is not None:
        store = WorkItemStore(workitems_dir)
    else:
        raise TypeError("workitems or workitems_dir is required")
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
        with sstore.locked(item.spec_id) as artifact:
            if artifact is None or artifact.spec.status not in ("approved", "dispatched"):
                return
            spec = artifact.spec
            now = datetime.now(timezone.utc)
            spec.status = "implemented"
            spec.updated_at = now
            sstore.save_while_locked(spec, artifact)
        # Bubble the freshly-done WorkItem to the top of list()'s updated_at-desc
        # view too (mirrors the spec-less phase_override bump below). The override
        # stays None — the spec drives `done`; this only refreshes updated_at.
        store.set_phase_override(wid, None, now=now)
        return

    # Spec-less: stamp done directly. The updated_at bump (mirrors
    # advance_spec_on_close) keeps the freshly-`done` item at the top of
    # WorkItemStore.list()'s updated_at-desc view rather than buried.
    store.set_phase_override(wid, "done", now=datetime.now(timezone.utc))
