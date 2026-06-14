from __future__ import annotations
from mship.core.spec import Spec


def approval_blockers(spec: Spec) -> list[str]:
    """Reasons a spec can't be approved (empty list == approvable)."""
    blockers: list[str] = []
    if not spec.acceptance_criteria:
        blockers.append("no acceptance criteria")
    bad = [c.id for c in spec.acceptance_criteria if c.verdict != "approved"]
    if bad:
        blockers.append(f"acceptance criteria not approved: {', '.join(bad)}")
    unanswered = [q.id for q in spec.open_questions if q.answer is None]
    if unanswered:
        blockers.append(f"open questions unanswered: {', '.join(unanswered)}")
    return blockers
