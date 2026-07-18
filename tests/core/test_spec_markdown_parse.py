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


def test_parses_list_sections_stripping_checkboxes_and_ids():
    text = (
        "## Problem\n\nP\n\n"
        "## User story\n\nU\n\n"
        "## Approach\n\nA\n\n"
        "## Acceptance criteria\n\n"
        "- [ ] `ac1` view questions\n"
        "- [x] `ac2` record answer\n\n"
        "## Open questions\n\n"
        "- [q1] Android in v0?\n\n"
        "## Non-goals\n\n"
        "- chat\n\n"
        "## Risks\n\n"
        "- scope creep\n\n"
        "## Affected repos\n\n"
        "- mothership\n"
    )
    draft = parse_spec_markdown(text)
    # Text only — ids/checkboxes stripped, matching the JSON path's text-only lists.
    assert draft.acceptance_criteria == ["view questions", "record answer"]
    assert draft.open_questions == ["Android in v0?"]
    assert draft.non_goals == ["chat"]
    assert draft.risks == ["scope creep"]
    assert draft.affected_repos == ["mothership"]
