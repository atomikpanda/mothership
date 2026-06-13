from datetime import datetime, timezone

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec


def _spec(**kw):
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    base = dict(id="demo", title="Demo", status="drafting", created_at=now, updated_at=now)
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
