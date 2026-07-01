# src/mship/core/view/workitem_index.py
from __future__ import annotations

from dataclasses import dataclass

from mship.core.message import Thread
from mship.core.spec import Spec
from mship.core.state import Task
from mship.core.workitem import Phase, WorkItem

_SPEC_PHASE: dict[str, Phase] = {
    "captured": "inbox",
    "drafting": "shaping",
    "needs_review": "shaping",
    "needs_clarification": "shaping",
    "approved": "ready",
    "dispatched": "in_flight",
    "implemented": "done",
    "archived": "done",
}


def compute_phase(item: WorkItem, spec: Spec | None, tasks: list[Task]) -> Phase:
    if item.phase_override is not None:
        return item.phase_override
    if tasks:
        if any(t.finished_at is None for t in tasks):
            return "in_flight"
        if any(t.pr_urls for t in tasks):
            return "review"
        return "done"
    if spec is not None:
        return _SPEC_PHASE.get(spec.status, "shaping")
    return "inbox"


@dataclass(frozen=True)
class Attention:
    needs_approval: bool
    needs_decision: bool
    blocked: bool
    needs_review: bool
    blocked_tasks: int
    total_tasks: int


def compute_attention(spec: Spec | None, tasks: list[Task], threads: list[Thread]) -> Attention:
    blocked_tasks = sum(1 for t in tasks if t.blocked_reason is not None)
    return Attention(
        needs_approval=spec is not None and spec.status == "needs_review",
        needs_decision=any(t.needs_you for t in threads),
        blocked=blocked_tasks > 0,
        needs_review=any(bool(t.pr_urls) for t in tasks),
        blocked_tasks=blocked_tasks,
        total_tasks=len(tasks),
    )
