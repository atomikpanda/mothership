from __future__ import annotations
from mship.core.spec import Spec
from mship.core.spec_review import PROSE_UNIT_IDS


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
    # Prose-section verdicts (MOS-172), backward-compatibly: a KNOWN section with an
    # explicit non-approved verdict blocks; a section absent from prose_verdicts
    # contributes nothing — legacy specs (and specs the reviewer hasn't touched)
    # still approve. Only known section ids (PROSE_UNIT_IDS) are settable/clearable
    # via the API, so a stray/unknown persisted key must NOT block — otherwise the
    # spec would be both un-approvable and un-fixable (Greptile #344).
    bad_prose = [
        sid for sid, pv in spec.prose_verdicts.items()
        if sid in PROSE_UNIT_IDS and pv.verdict != "approved"
    ]
    if bad_prose:
        blockers.append(f"prose sections not approved: {', '.join(sorted(bad_prose))}")
    return blockers
