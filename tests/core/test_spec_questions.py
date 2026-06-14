from datetime import datetime, timezone
import pytest
from mship.core.spec import OpenQuestion, Spec
from mship.core.spec_questions import add_question, answer_question, list_questions


def _spec(qs=None):
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    return Spec(id="dq", title="DQ", status="needs_review", created_at=now, updated_at=now,
                open_questions=qs or [])


def test_add_question_assigns_sequential_ids():
    s = _spec()
    assert add_question(s, "first").id == "q1"
    assert add_question(s, "second").id == "q2"


def test_add_question_continues_after_existing():
    s = _spec([OpenQuestion(id="q1", text="seeded")])
    assert add_question(s, "next").id == "q2"


def test_answer_question_sets_and_rejects_unknown():
    s = _spec([OpenQuestion(id="q1", text="?")])
    answer_question(s, "q1", "yes")
    assert s.open_questions[0].answer == "yes"
    with pytest.raises(ValueError):
        answer_question(s, "q9", "x")


def test_list_questions_shape():
    s = _spec([OpenQuestion(id="q1", text="?", answer="a")])
    assert list_questions(s) == [{"id": "q1", "text": "?", "answer": "a"}]
