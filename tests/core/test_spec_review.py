import pytest

from datetime import datetime, timezone

from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, OpenQuestion, Spec
from mship.core.spec_body import render_body
from mship.core.spec_review import (
    build_review, infer_evidence_kind, set_criterion_evidence, set_criterion_verdict,
)


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
        {"id": "ac1", "text": "view questions", "verdict": "approved", "comment": None, "evidence": []},
        {"id": "ac2", "text": "record answer", "verdict": "unreviewed", "comment": None, "evidence": []},
    ]
    assert r["open_questions"] == [{"id": "q1", "text": "Android?", "answer": None}]
    assert r["context"]["problem"] == "the problem"
    assert r["context"]["approach"] == "the approach"
    assert r["context"]["non_goals"] == ["chat"]


def test_build_review_emits_prose_verdicts_and_comments():
    from mship.core.spec import ProseVerdict
    s = _spec()
    set_criterion_verdict(s, "ac1", "flagged", comment="fix")
    s.prose_verdicts = {"approach": ProseVerdict(verdict="approved")}
    review = build_review(s)
    assert review["prose_verdicts"]["approach"]["verdict"] == "approved"
    # the criterion's comment is exposed in the review's criteria list
    crit = next(c for c in review["acceptance_criteria"] if c["id"] == "ac1")
    assert crit["comment"] == "fix"


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
        "unverified": 2, "open_questions_unanswered": 1,
    }


def test_build_review_surfaces_evidence_and_unverified_count():
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    spec = Spec(
        id="dq", title="DQ", status="needs_review", created_at=now, updated_at=now,
        acceptance_criteria=[
            AcceptanceCriterion(id="ac1", text="a", verdict="approved",
                                evidence=[AcceptanceEvidence(kind="test", ref="test-runs/5")]),
            AcceptanceCriterion(id="ac2", text="b"),   # no evidence
            AcceptanceCriterion(id="ac3", text="c"),   # no evidence
        ],
    )
    r = build_review(spec)
    assert r["acceptance_criteria"][0]["evidence"] == [
        {"kind": "test", "ref": "test-runs/5", "note": None}
    ]
    assert r["acceptance_criteria"][1]["evidence"] == []
    # unverified is EXACTLY the number of ACs with an empty evidence list.
    assert r["summary"]["unverified"] == 2


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


def test_set_prose_verdict_accepts_a_prose_section():
    from mship.core.spec_review import set_prose_verdict
    s = _spec()
    set_prose_verdict(s, "approach", "flagged", comment="unclear")
    assert s.prose_verdicts["approach"].verdict == "flagged"
    assert s.prose_verdicts["approach"].comment == "unclear"


def test_set_prose_verdict_rejects_unknown_section():
    from mship.core.spec_review import set_prose_verdict
    import pytest
    with pytest.raises(ValueError, match="not a prose section"):
        set_prose_verdict(_spec(), "bogus", "approved")


def test_set_prose_verdict_rejects_bad_verdict():
    from mship.core.spec_review import set_prose_verdict
    import pytest
    with pytest.raises(ValueError, match="invalid verdict"):
        set_prose_verdict(_spec(), "problem", "bogus")


def test_set_criterion_verdict_stores_comment():
    s = _spec()  # _spec() should include an ac1
    set_criterion_verdict(s, "ac1", "flagged", comment="needs work")
    assert s.acceptance_criteria[0].comment == "needs work"


def test_set_criterion_evidence_appends_and_persists_in_object():
    spec = _spec()
    set_criterion_evidence(spec, "ac2", "test", "test-runs/5.mothership", note="ran it")
    ev = spec.acceptance_criteria[1].evidence
    assert ev == [AcceptanceEvidence(kind="test", ref="test-runs/5.mothership", note="ran it")]


def test_set_criterion_evidence_rejects_bad_kind():
    with pytest.raises(ValueError, match="kind"):
        set_criterion_evidence(_spec(), "ac1", "screenshot", "x")


def test_set_criterion_evidence_rejects_unknown_id():
    with pytest.raises(ValueError):
        set_criterion_evidence(_spec(), "ac99", "commit", "deadbeef")


@pytest.mark.parametrize("ref,expected", [
    ("test-runs/5", "test"),
    ("test-runs/5.mothership", "test"),
    ("deadbeefcafe", "commit"),
    ("a1b2c3d", "commit"),
    ("docs/design.md:12-18", "artifact"),
    ("https://example.com/run/9", "artifact"),
    ("HEAD", "artifact"),
])
def test_infer_evidence_kind(ref, expected):
    assert infer_evidence_kind(ref) == expected
