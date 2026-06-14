from mship.core.spec_draft import build_draft_prompt


def test_build_draft_prompt_contains_intent_schema_and_apply():
    prompt = build_draft_prompt("decision-queue", "I want X away from the desk")
    assert "decision-queue" in prompt
    assert "I want X away from the desk" in prompt          # the intent
    assert "acceptance_criteria" in prompt                  # the JSON shape
    assert "open_questions" in prompt
    assert "mship spec apply decision-queue --from-json" in prompt  # how to apply
    assert "only" in prompt.lower()                         # "output only JSON"
