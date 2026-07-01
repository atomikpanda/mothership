# src/mship/core/view/workitem_index.py
from __future__ import annotations

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
