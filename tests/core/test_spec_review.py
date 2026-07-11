import pytest

from datetime import datetime, timezone

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec
from mship.core.spec_body import render_body
from mship.core.spec_review import build_review, set_criterion_verdict


def _spec():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    return Spec(
        id="dq", title="DQ", status="needs_review", created_at=now, updated_at=now,
        body=render_body("the problem", "as a user", "the approach"),
        non_goals=["chat"], risks=["scope"], affected_repos=["mothership"],
        acceptance_criteria=[
            AcceptanceCriterion(id="ac1", text="view questions", verdict="approved"),
            AcceptanceCriterion(id="ac2", text="record answer"),
        ],
        open_questions=[OpenQuestion(id="q1", text="Android?")],
    )


def test_build_review_shapes_units_and_context():
    r = build_review(_spec())
    assert r["id"] == "dq" and r["status"] == "needs_review"
    assert r["acceptance_criteria"] == [
        {"id": "ac1", "text": "view questions", "verdict": "approved"},
        {"id": "ac2", "text": "record answer", "verdict": "unreviewed"},
    ]
    assert r["open_questions"] == [{"id": "q1", "text": "Android?", "answer": None}]
    assert r["context"]["problem"] == "the problem"
    assert r["context"]["approach"] == "the approach"
    assert r["context"]["non_goals"] == ["chat"]


def test_build_review_includes_clarification_reason():
    spec = _spec()
    spec.clarification_reason = "tighten scope"
    r = build_review(spec)
    assert r["clarification_reason"] == "tighten scope"


def test_build_review_clarification_reason_defaults_none():
    assert build_review(_spec())["clarification_reason"] is None


def test_build_review_summary_counts():
    s = build_review(_spec())["summary"]
    assert s == {
        "criteria_total": 2, "approved": 1, "flagged": 0, "unreviewed": 1,
        "open_questions_unanswered": 1,
    }


def test_set_criterion_verdict_updates():
    spec = _spec()
    set_criterion_verdict(spec, "ac2", "flagged")
    assert spec.acceptance_criteria[1].verdict == "flagged"


def test_set_criterion_verdict_rejects_bad_verdict():
    with pytest.raises(ValueError):
        set_criterion_verdict(_spec(), "ac1", "bogus")


def test_set_criterion_verdict_rejects_unknown_id():
    with pytest.raises(ValueError):
        set_criterion_verdict(_spec(), "nope", "approved")


def test_set_criterion_verdict_rejects_prose_unit():
    with pytest.raises(ValueError, match="not verdict-able"):
        set_criterion_verdict(_spec(), "problem", "approved")
