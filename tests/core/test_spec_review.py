from datetime import datetime, timezone

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec
from mship.core.spec_body import render_body
from mship.core.spec_review import build_review


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


def test_build_review_summary_counts():
    s = build_review(_spec())["summary"]
    assert s == {
        "criteria_total": 2, "approved": 1, "flagged": 0, "unreviewed": 1,
        "open_questions_unanswered": 1,
    }
