from datetime import datetime, timezone
from mship.core.spec import AcceptanceCriterion, ProseVerdict, Spec


def _now():
    return datetime(2026, 7, 13, tzinfo=timezone.utc)


def test_spec_carries_prose_verdicts_and_criterion_comment():
    s = Spec(
        id="s1", title="T", status="needs_review", created_at=_now(), updated_at=_now(),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="flagged", comment="fix this")],
        prose_verdicts={"problem": ProseVerdict(verdict="approved"),
                        "approach": ProseVerdict(verdict="flagged", comment="unclear")},
    )
    assert s.prose_verdicts["approach"].comment == "unclear"
    assert s.acceptance_criteria[0].comment == "fix this"
    # round-trips through model_dump (what GET /specs/{id} + frontmatter use)
    dumped = s.model_dump(mode="json")
    assert dumped["prose_verdicts"]["problem"]["verdict"] == "approved"
    assert dumped["acceptance_criteria"][0]["comment"] == "fix this"


def test_prose_verdicts_defaults_empty():
    s = Spec(id="s1", title="T", status="draft", created_at=_now(), updated_at=_now())
    assert s.prose_verdicts == {}
