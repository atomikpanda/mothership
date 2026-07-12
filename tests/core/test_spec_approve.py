from datetime import datetime, timezone
from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec
from mship.core.spec_approve import approval_blockers


def _spec(criteria=None, questions=None):
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    return Spec(id="dq", title="DQ", status="needs_review", created_at=now, updated_at=now,
                acceptance_criteria=criteria or [], open_questions=questions or [])


def test_blockers_flag_unapproved_criteria():
    s = _spec(criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="flagged")])
    assert any("ac1" in b for b in approval_blockers(s))


def test_blockers_flag_unanswered_questions():
    s = _spec(criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="approved")],
              questions=[OpenQuestion(id="q1", text="?")])
    assert any("q1" in b for b in approval_blockers(s))


def test_blockers_flag_no_criteria():
    assert approval_blockers(_spec()) != []


def test_no_blockers_when_all_clear():
    s = _spec(criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="approved")],
              questions=[OpenQuestion(id="q1", text="?", answer="yes")])
    assert approval_blockers(s) == []


def test_approval_gate_unchanged_approved_verdicts_zero_evidence():
    """ac6: evidence is NEVER required to approve. A spec with all verdicts
    approved and NO evidence has no approval blockers."""
    s = _spec(criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="approved")])
    assert all(c.evidence == [] for c in s.acceptance_criteria)
    assert approval_blockers(s) == []
