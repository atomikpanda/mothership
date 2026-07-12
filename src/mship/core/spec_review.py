from __future__ import annotations

import re

from mship.core.spec import AcceptanceEvidence, Spec
from mship.core.spec_body import parse_body_sections

VERDICTS: tuple[str, ...] = ("unreviewed", "approved", "flagged")
PROSE_UNIT_IDS: frozenset[str] = frozenset(
    {"problem", "user_story", "approach", "non_goals", "risks", "scope_risk"}
)
EVIDENCE_KINDS: tuple[str, ...] = ("test", "commit", "artifact")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)


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
        "clarification_reason": spec.clarification_reason,
        "acceptance_criteria": [
            {
                "id": c.id,
                "text": c.text,
                "verdict": c.verdict,
                "evidence": [
                    {"kind": e.kind, "ref": e.ref, "note": e.note} for e in c.evidence
                ],
            }
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
            "unverified": sum(1 for c in spec.acceptance_criteria if not c.evidence),
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
    if criterion_id in PROSE_UNIT_IDS:
        raise ValueError(
            f"{criterion_id!r} is not verdict-able; only acceptance criteria "
            f"(ac1, ac2, …) carry verdicts in this version."
        )
    for c in spec.acceptance_criteria:
        if c.id == criterion_id:
            c.verdict = verdict
            return spec
    valid = ", ".join(c.id for c in spec.acceptance_criteria) or "(none)"
    raise ValueError(f"no acceptance criterion {criterion_id!r}; valid ids: {valid}")


def infer_evidence_kind(ref: str) -> str:
    """Infer an evidence kind from a ref's shape (the `mship debug --evidence`
    convention): `test-runs/…` → test; a 7–40 char hex sha → commit; else artifact.
    Refs are advisory — never resolved or validated. Callers pass an explicit
    kind to override (e.g. `HEAD`, which is not hex, defaults to artifact here)."""
    if ref.startswith("test-runs/"):
        return "test"
    if _SHA_RE.match(ref):
        return "commit"
    return "artifact"


def set_criterion_evidence(
    spec: Spec, criterion_id: str, kind: str, ref: str, note: str | None = None,
) -> Spec:
    """Append one evidence entry to an acceptance criterion in place. Raises
    ValueError on an invalid kind or unknown criterion id (mirrors
    set_criterion_verdict). Does not change status or persist."""
    if kind not in EVIDENCE_KINDS:
        raise ValueError(
            f"invalid evidence kind {kind!r}; expected one of {', '.join(EVIDENCE_KINDS)}"
        )
    if criterion_id in PROSE_UNIT_IDS:
        raise ValueError(
            f"{criterion_id!r} is not an acceptance criterion; only acceptance "
            f"criteria (ac1, ac2, …) carry evidence in this version."
        )
    for c in spec.acceptance_criteria:
        if c.id == criterion_id:
            c.evidence.append(AcceptanceEvidence(kind=kind, ref=ref, note=note))
            return spec
    valid = ", ".join(c.id for c in spec.acceptance_criteria) or "(none)"
    raise ValueError(f"no acceptance criterion {criterion_id!r}; valid ids: {valid}")
