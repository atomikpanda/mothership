"""One-line WorkItem/phase-aware header strings for the view commands (AC9).

Pure: operate on the `WorkItemSummary` index (already carries derived `phase`,
`title`, `spec_id`, `task_slugs`). Return None when nothing links, so callers
simply omit the header and keep their current output.
"""
from __future__ import annotations

from mship.core.view.workitem_index import WorkItemSummary


def _base(wi: WorkItemSummary) -> str:
    parts = [f"◆ {wi.id}"]
    if wi.title:
        parts.append(wi.title)
    parts.append(f"[{wi.phase}]")
    return "  ·  ".join(parts)


def header_for_task(task_slug: str, task_phase: str | None,
                    workitems: list[WorkItemSummary]) -> str | None:
    """Header for a task-scoped view (journal, diff). None when the task belongs
    to no WorkItem. Appends the task's own phase when known."""
    wi = next((w for w in workitems if task_slug in w.task_slugs), None)
    if wi is None:
        return None
    line = _base(wi)
    if task_phase is not None:
        line += f"  —  task {task_slug} [{task_phase}]"
    return line


def header_for_spec(spec_id: str, workitems: list[WorkItemSummary]) -> str | None:
    """Header for the spec view: the WorkItem that links this spec. None when
    the spec is unlinked."""
    wi = next((w for w in workitems if w.spec_id == spec_id), None)
    return _base(wi) if wi is not None else None
