from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, InvalidTransition, OpenQuestion, Spec, can_transition, validate_transition


def _spec(**kw):
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    base = dict(id="demo", title="Demo", status="draft", created_at=now, updated_at=now)
    base.update(kw)
    return Spec(**base)


def test_spec_defaults_are_empty():
    s = _spec()
    assert s.affected_repos == []
    assert s.acceptance_criteria == []
    assert s.open_questions == []
    assert s.non_goals == []
    assert s.risks == []
    assert s.task_slug is None
    assert s.body == ""


def test_dispatch_ready_requires_approved_and_no_open_questions():
    s = _spec(status="approved", open_questions=[OpenQuestion(id="q1", text="?", answer=None)])
    assert s.dispatch_ready is False
    s2 = _spec(status="approved", open_questions=[OpenQuestion(id="q1", text="?", answer="yes")])
    assert s2.dispatch_ready is True
    s3 = _spec(status="needs_review")
    assert s3.dispatch_ready is False


def test_acceptance_criterion_verdict_defaults_unreviewed():
    ac = AcceptanceCriterion(id="ac1", text="works")
    assert ac.verdict == "unreviewed"


def test_acceptance_evidence_defaults_note_none():
    e = AcceptanceEvidence(kind="artifact", ref="docs/x.md:12-18")
    assert e.note is None


def test_acceptance_evidence_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        AcceptanceEvidence(kind="screenshot", ref="x")


@pytest.mark.parametrize("current,target", [
    ("draft", "needs_review"),
    ("needs_review", "approved"),
    ("needs_review", "draft"),              # request-changes / send back
    ("approved", "dispatched"),
    ("approved", "draft"),                  # re-open / request-changes
    ("dispatched", "implemented"),
    ("implemented", "archived"),
    ("draft", "archived"),                 # abandon from any non-terminal
    ("approved", "archived"),              # abandon
    ("needs_review", "archived"),          # abandon
    ("dispatched", "archived"),            # abandon
])
def test_legal_transitions_allowed(current, target):
    assert can_transition(current, target) is True
    validate_transition(current, target)  # must not raise


@pytest.mark.parametrize("current,target", [
    ("draft", "approved"),        # skips review
    ("draft", "dispatched"),      # skips review/approval
    ("archived", "draft"),        # terminal
    ("approved", "approved"),     # no-op
])
def test_illegal_transitions_rejected(current, target):
    assert can_transition(current, target) is False
    with pytest.raises(InvalidTransition):
        validate_transition(current, target)


def test_spec_draft_defaults():
    from mship.core.spec import SpecDraft
    d = SpecDraft(problem="p", user_story="u", approach="a")
    assert d.non_goals == []
    assert d.risks == []
    assert d.affected_repos == []
    assert d.acceptance_criteria == []
    assert d.open_questions == []


def test_spec_draft_accepts_lists():
    from mship.core.spec import SpecDraft
    d = SpecDraft(
        problem="p", user_story="u", approach="a",
        acceptance_criteria=["c1", "c2"], open_questions=["q1"],
        non_goals=["ng"], risks=["r"], affected_repos=["mothership"],
    )
    assert d.acceptance_criteria == ["c1", "c2"]
    assert d.open_questions == ["q1"]
