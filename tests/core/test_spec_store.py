from datetime import datetime, timezone

import pytest

from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec
from mship.core.spec_store import SpecParseError, parse_spec, serialize_spec


def _spec():
    now = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
    return Spec(
        id="decision-queue", title="Decision queue", status="needs_review",
        created_at=now, updated_at=now,
        affected_repos=["mothership", "ground-control"],
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="view questions")],
        open_questions=[OpenQuestion(id="q1", text="Android in v0?")],
        non_goals=["chat"],
        body="## Problem\n\nAgents block away from the desk.\n",
    )


def test_round_trip_is_identity():
    s = _spec()
    assert parse_spec(serialize_spec(s)) == s


def test_body_is_preserved_verbatim():
    s = _spec()
    parsed = parse_spec(serialize_spec(s))
    assert parsed.body == "## Problem\n\nAgents block away from the desk.\n"


def test_missing_frontmatter_raises():
    with pytest.raises(SpecParseError):
        parse_spec("# just markdown, no frontmatter\n")


def test_unterminated_frontmatter_raises():
    with pytest.raises(SpecParseError):
        parse_spec("---\nid: foo\n")  # no closing ---


def test_invalid_schema_frontmatter_raises_spec_parse_error():
    # valid YAML, but missing required Spec fields -> SpecParseError, not raw ValidationError
    with pytest.raises(SpecParseError):
        parse_spec("---\nid: foo\n---\nbody\n")


def test_malformed_yaml_raises_spec_parse_error():
    with pytest.raises(SpecParseError):
        parse_spec("---\nid: [unclosed\n---\nbody\n")
