from __future__ import annotations

from mship.core.spec import Spec
from mship.core.spec_body import parse_body_sections

VERDICTS: tuple[str, ...] = ("unreviewed", "approved", "flagged")


def build_review(spec: Spec) -> dict:
    """Strictly-factual review payload for the Ground Control review cards.

    Quotes the spec verbatim — no inference. Prose context is read-only;
    only acceptance criteria carry verdicts (see design A3)."""
    sections = parse_body_sections(spec.body)
    counts = {v: 0 for v in VERDICTS}
    for c in spec.acceptance_criteria:
        counts[c.verdict] = counts.get(c.verdict, 0) + 1
    return {
        "id": spec.id,
        "status": spec.status,
        "acceptance_criteria": [
            {"id": c.id, "text": c.text, "verdict": c.verdict}
            for c in spec.acceptance_criteria
        ],
        "open_questions": [
            {"id": q.id, "text": q.text, "answer": q.answer}
            for q in spec.open_questions
        ],
        "context": {
            "problem": sections.get("Problem", ""),
            "user_story": sections.get("User story", ""),
            "approach": sections.get("Approach", ""),
            "non_goals": list(spec.non_goals),
            "risks": list(spec.risks),
            "affected_repos": list(spec.affected_repos),
        },
        "summary": {
            "criteria_total": len(spec.acceptance_criteria),
            "approved": counts["approved"],
            "flagged": counts["flagged"],
            "unreviewed": counts["unreviewed"],
            "open_questions_unanswered": sum(
                1 for q in spec.open_questions if q.answer is None
            ),
        },
    }


def set_criterion_verdict(spec: Spec, criterion_id: str, verdict: str) -> Spec:
    """Set one acceptance criterion's verdict in place. Raises ValueError on an
    invalid verdict or unknown criterion id. Does not change status or persist."""
    if verdict not in VERDICTS:
        raise ValueError(
            f"invalid verdict {verdict!r}; expected one of {', '.join(VERDICTS)}"
        )
    for c in spec.acceptance_criteria:
        if c.id == criterion_id:
            c.verdict = verdict
            return spec
    valid = ", ".join(c.id for c in spec.acceptance_criteria) or "(none)"
    raise ValueError(f"no acceptance criterion {criterion_id!r}; valid ids: {valid}")
