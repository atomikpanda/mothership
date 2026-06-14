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

from mship.core.spec import Spec, SpecDraft
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
