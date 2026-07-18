from mship.core.spec import SpecDraft
from mship.core.spec_draft import parse_spec_markdown


def test_parses_required_prose_sections_into_empty_draft():
    text = (
        "## Problem\n\nP\n\n"
        "## User story\n\nU\n\n"
        "## Approach\n\nA\n"
    )
    draft = parse_spec_markdown(text)
    assert isinstance(draft, SpecDraft)
    assert draft.problem == "P"
    assert draft.user_story == "U"
    assert draft.approach == "A"
    # Optional fields default empty when their sections are absent.
    assert draft.acceptance_criteria == []
    assert draft.open_questions == []
    assert draft.non_goals == []
    assert draft.risks == []
    assert draft.affected_repos == []
    assert draft.additional_sections == []
