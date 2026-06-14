from __future__ import annotations

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec, SpecDraft
from mship.core.spec_body import render_body


def build_draft_prompt(spec_id: str, intent_text: str) -> str:
    """Agent-agnostic prompt: turn intent into a SpecDraft JSON. No model call."""
    return f"""\
# Draft spec `{spec_id}`

Turn the intent below into a structured spec. Output **only** a single JSON
object matching this shape — no prose, no markdown fence:

{{
  "problem": "<why this matters>",
  "user_story": "As a <user>, I want <capability>, so that <benefit>.",
  "approach": "<how it works; key decisions>",
  "non_goals": ["<explicitly out of scope>"],
  "risks": ["<known risk>"],
  "affected_repos": ["<repo>"],
  "acceptance_criteria": ["<testable, user-visible outcome>"],
  "open_questions": ["<must be resolved before approval>"]
}}

## Intent

{intent_text}

## Apply your result

Save the JSON to a file and run:

    mship spec apply {spec_id} --from-json <file>

or pipe it directly:

    cat draft.json | mship spec apply {spec_id} --from-json -
"""
