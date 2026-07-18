from datetime import datetime, timezone

from mship.core.evidence_autolink import extract_ac_ids
from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, Spec


def _spec(criteria):
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    return Spec(id="s1", title="S", status="approved", created_at=now,
                updated_at=now, acceptance_criteria=criteria)


def test_extract_ac_ids_standalone_tokens_lowercased():
    assert extract_ac_ids("fixes ac1 and AC3") == {"ac1", "ac3"}


def test_extract_ac_ids_word_boundary_excludes_longer_id_and_substrings():
    # `ac7` written alone must not surface `ac70` (\d+ is greedy to a boundary);
    # `ac7` buried in `mac7book` and the letters of `reactor` are not references.
    assert extract_ac_ids("done ac7") == {"ac7"}
    assert extract_ac_ids("done ac70") == {"ac70"}
    assert extract_ac_ids("patch mac7book near the reactor") == set()


def test_extract_ac_ids_empty_message():
    assert extract_ac_ids("") == set()
