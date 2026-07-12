from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, OpenQuestion, Spec
from mship.core.spec_store import SpecParseError, SpecStore, parse_spec, serialize_spec


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


def test_evidence_round_trips_through_serialize_parse():
    now = datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc)
    s = Spec(
        id="ev", title="Evidence", status="needs_review",
        created_at=now, updated_at=now,
        acceptance_criteria=[AcceptanceCriterion(
            id="ac1", text="does the thing", verdict="approved",
            evidence=[
                AcceptanceEvidence(kind="test", ref="test-runs/5.mothership"),
                AcceptanceEvidence(kind="commit", ref="deadbeef", note="the fix"),
            ],
        )],
        body="## Problem\n\nx\n",
    )
    parsed = parse_spec(serialize_spec(s))
    assert parsed == s
    assert parsed.acceptance_criteria[0].evidence[1].note == "the fix"


def test_legacy_spec_without_evidence_key_loads_with_empty_list():
    # A frontmatter block whose acceptance_criteria have NO evidence key at all
    # (an older on-disk spec) must load with evidence == [].
    text = (
        "---\n"
        "id: legacy\n"
        "title: Legacy\n"
        "status: needs_review\n"
        "created_at: '2026-07-12T10:00:00Z'\n"
        "updated_at: '2026-07-12T10:00:00Z'\n"
        "acceptance_criteria:\n"
        "- id: ac1\n"
        "  text: old criterion\n"
        "  verdict: approved\n"
        "---\n"
        "## Problem\n\nlegacy body\n"
    )
    spec = parse_spec(text)
    assert spec.acceptance_criteria[0].evidence == []


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


def _new_spec(spec_id: str):
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    return Spec(id=spec_id, title=spec_id, status="drafting", created_at=now, updated_at=now)


def test_save_then_find_by_id(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    path = store.save(_new_spec("alpha"))
    assert path.name == "2026-06-13-alpha.md"
    assert path.is_file()
    found = store.find_by_id("alpha")
    assert found is not None and found.id == "alpha"


def test_find_by_id_is_exact_not_mtime(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    store.save(_new_spec("alpha"))
    store.save(_new_spec("beta"))   # newer mtime
    assert store.find_by_id("alpha").id == "alpha"


def test_list_returns_all(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    store.save(_new_spec("alpha"))
    store.save(_new_spec("beta"))
    assert sorted(s.id for s in store.list()) == ["alpha", "beta"]


def test_find_by_id_missing_returns_none(tmp_path: Path):
    assert SpecStore(tmp_path / "specs").find_by_id("nope") is None


def test_save_overwrites_and_reflects_update(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    store.save(_new_spec("alpha"))
    updated = _new_spec("alpha")
    updated.status = "needs_review"
    store.save(updated)
    assert store.find_by_id("alpha").status == "needs_review"
    assert len(store.list()) == 1  # same path, overwritten not duplicated


def test_path_for_rejects_unsafe_id(tmp_path: Path):
    store = SpecStore(tmp_path / "specs")
    bad = _new_spec("alpha")
    bad.id = "../escape"
    with pytest.raises(ValueError):
        store.save(bad)
