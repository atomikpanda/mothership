"""Group task summaries under their WorkItem for `mship view status` (AC5).

Pure: takes the task index (list[TaskSummary]) and the WorkItem index
(list[WorkItemSummary]) and returns ordered groups. Tasks keep their own phase;
the group header carries the WorkItem's derived phase.
"""
from __future__ import annotations

from dataclasses import dataclass

from mship.core.view.task_index import TaskSummary
from mship.core.view.workitem_index import WorkItemSummary


@dataclass(frozen=True)
class WorkItemTaskGroup:
    work_item_id: str | None
    title: str | None
    phase: str | None
    tasks: list[TaskSummary]


def group_tasks_by_workitem(
    tasks: list[TaskSummary],
    workitems: list[WorkItemSummary],
) -> list[WorkItemTaskGroup]:
    """Group tasks under their WorkItem. WorkItem order follows `workitems`
    (already active-before-done from build_workitem_index); tasks linked to no
    WorkItem fall into a single trailing group with work_item_id=None. Each task
    keeps its own `phase` (rendered by the caller)."""
    by_slug = {t.slug: t for t in tasks}
    groups: list[WorkItemTaskGroup] = []
    claimed: set[str] = set()
    for wi in workitems:
        members = [by_slug[s] for s in wi.task_slugs if s in by_slug]
        if not members:
            continue
        claimed.update(t.slug for t in members)
        groups.append(WorkItemTaskGroup(
            work_item_id=wi.id, title=wi.title, phase=wi.phase, tasks=members,
        ))
    ungrouped = [t for t in tasks if t.slug not in claimed]
    if ungrouped:
        groups.append(WorkItemTaskGroup(
            work_item_id=None, title=None, phase=None, tasks=ungrouped,
        ))
    return groups
