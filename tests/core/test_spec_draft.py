from mship.core.spec_draft import build_draft_prompt


def test_build_draft_prompt_contains_intent_schema_and_apply():
    prompt = build_draft_prompt("decision-queue", "I want X away from the desk")
    assert "decision-queue" in prompt
    assert "I want X away from the desk" in prompt          # the intent
    assert "acceptance_criteria" in prompt                  # the JSON shape
    assert "open_questions" in prompt
    assert "mship spec apply decision-queue --from-json" in prompt  # how to apply
    assert "only" in prompt.lower()                         # "output only JSON"


from datetime import datetime, timezone

from mship.core.spec import (
    AcceptanceCriterion,
    AcceptanceEvidence,
    Spec,
    SpecDraft,
)
from mship.core.spec_draft import apply_draft
from mship.core.spec_body import validate_body_structure


def _spec():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    return Spec(id="dq", title="DQ", status="drafting", created_at=now, updated_at=now,
                task_slug="dq")


def test_apply_draft_merges_fields_and_assigns_ids():
    spec = _spec()
    draft = SpecDraft(
        problem="P", user_story="U", approach="A",
        non_goals=["chat"], risks=["scope"], affected_repos=["mothership"],
        acceptance_criteria=["view questions", "record answer"],
        open_questions=["Android in v0?"],
    )
    out = apply_draft(spec, draft)
    assert validate_body_structure(out.body) == []          # canonical body rendered
    assert out.non_goals == ["chat"] and out.affected_repos == ["mothership"]
    assert [c.id for c in out.acceptance_criteria] == ["ac1", "ac2"]
    assert out.acceptance_criteria[0].text == "view questions"
    assert out.acceptance_criteria[0].verdict == "unreviewed"
    assert [q.id for q in out.open_questions] == ["q1"]
    assert out.open_questions[0].answer is None
    assert out.id == "dq" and out.task_slug == "dq"         # identity preserved


def test_apply_draft_preserves_evidence_and_verdict_for_unchanged_ac():
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(
            id="ac1", text="view questions", verdict="approved",
            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/5")],
        ),
    ]
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      acceptance_criteria=["view questions"])   # SAME text
    out = apply_draft(spec, draft)
    assert out.acceptance_criteria[0].verdict == "approved"     # preserved
    assert out.acceptance_criteria[0].evidence == [AcceptanceEvidence(kind="test", ref="test-runs/5")]


def test_apply_draft_resets_evidence_and_verdict_for_materially_changed_ac():
    spec = _spec()
    spec.acceptance_criteria = [
        AcceptanceCriterion(
            id="ac1", text="view questions", verdict="approved",
            evidence=[AcceptanceEvidence(kind="test", ref="test-runs/5")],
        ),
    ]
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      acceptance_criteria=["view questions AND record answers"])  # CHANGED
    out = apply_draft(spec, draft)
    assert out.acceptance_criteria[0].verdict == "unreviewed"   # fresh
    assert out.acceptance_criteria[0].evidence == []            # fresh


import pytest

from mship.core.spec_draft import new_spec, SPEC_BODY_TEMPLATE


def test_new_spec_defaults_id_from_title():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    spec = new_spec("Decision Queue", now=now)
    assert spec.id == "decision-queue"          # slugified title
    assert spec.title == "Decision Queue"
    assert spec.status == "drafting"            # fresh specs start drafting
    assert spec.created_at == now and spec.updated_at == now
    assert spec.body == SPEC_BODY_TEMPLATE      # canonical empty body
    assert spec.affected_repos == [] and spec.task_slug is None


def test_new_spec_honors_explicit_id_repos_and_task():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    spec = new_spec("Anything", now=now, spec_id="custom",
                    affected_repos=["a", "b"], task_slug="t")
    assert spec.id == "custom"
    assert spec.affected_repos == ["a", "b"]
    assert spec.task_slug == "t"


def test_new_spec_unslugifiable_title_raises():
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        new_spec("!!!", now=now)             # slug collapses to empty


def test_specdraft_accepts_additional_sections():
    d = SpecDraft(problem="P", user_story="U", approach="A",
                  additional_sections=[{"heading": "Architecture", "body": "arch"}])
    assert d.additional_sections[0].heading == "Architecture"
    assert d.additional_sections[0].body == "arch"


def test_apply_draft_renders_additional_sections():
    from mship.core.spec_body import parse_body_sections, validate_body_structure
    spec = _spec()
    draft = SpecDraft(problem="P", user_story="U", approach="A",
                      additional_sections=[{"heading": "Testing", "body": "the tests"}])
    out = apply_draft(spec, draft)
    sections = parse_body_sections(out.body)
    assert sections["Testing"] == "the tests"
    assert validate_body_structure(out.body) == []   # required still present


def test_build_draft_prompt_mentions_additional_sections():
    assert "additional_sections" in build_draft_prompt("x", "intent")
