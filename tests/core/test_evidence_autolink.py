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


from mship.core.evidence_autolink import EvidenceLink, compute_evidence_links


def test_testrun_refs_attach_to_every_criterion():
    spec = _spec([AcceptanceCriterion(id="ac1", text="x"),
                  AcceptanceCriterion(id="ac2", text="y")])
    links = compute_evidence_links(spec, commits=[],
                                   test_run_refs=["test-runs/1.mothership"])
    assert set(links) == {
        EvidenceLink("ac1", "test", "test-runs/1.mothership"),
        EvidenceLink("ac2", "test", "test-runs/1.mothership"),
    }


def test_commit_attaches_to_named_criterion():  # ac2
    spec = _spec([AcceptanceCriterion(id="ac1", text="x"),
                  AcceptanceCriterion(id="ac2", text="y")])
    links = compute_evidence_links(spec, commits=[("sha1", "implement ac2 logic")],
                                   test_run_refs=[])
    assert links == [EvidenceLink("ac2", "commit", "sha1")]


def test_commit_naming_multiple_ids_attaches_to_all():  # ac3
    spec = _spec([AcceptanceCriterion(id="ac1", text="x"),
                  AcceptanceCriterion(id="ac3", text="z")])
    links = compute_evidence_links(spec, commits=[("sha1", "ac1 and ac3 together")],
                                   test_run_refs=[])
    assert set(links) == {EvidenceLink("ac1", "commit", "sha1"),
                          EvidenceLink("ac3", "commit", "sha1")}


def test_commit_naming_no_id_is_noop():  # ac4
    spec = _spec([AcceptanceCriterion(id="ac1", text="x")])
    links = compute_evidence_links(spec, commits=[("sha1", "refactor internals")],
                                   test_run_refs=[])
    assert links == []


def test_word_boundary_ac7_does_not_attach_to_ac70():  # ac5
    spec = _spec([AcceptanceCriterion(id="ac7", text="x"),
                  AcceptanceCriterion(id="ac70", text="y")])
    links = compute_evidence_links(spec, commits=[("sha1", "handles ac7")],
                                   test_run_refs=[])
    assert links == [EvidenceLink("ac7", "commit", "sha1")]


def test_word_boundary_substring_is_not_a_reference():  # ac5
    spec = _spec([AcceptanceCriterion(id="ac7", text="x")])
    links = compute_evidence_links(spec, commits=[("sha1", "patch mac7book in reactor")],
                                   test_run_refs=[])
    assert links == []


def test_unknown_ac_id_in_commit_is_ignored():  # ac5 (intersect with real ids)
    spec = _spec([AcceptanceCriterion(id="ac1", text="x")])
    links = compute_evidence_links(spec, commits=[("sha1", "touches ac9")],
                                   test_run_refs=[])
    assert links == []


from mship.core.spec_review import set_criterion_evidence


def test_skips_evidence_that_already_exists():  # ac6 (dedup vs existing)
    spec = _spec([AcceptanceCriterion(
        id="ac1", text="x",
        evidence=[AcceptanceEvidence(kind="test", ref="test-runs/1.mothership")])])
    links = compute_evidence_links(spec, commits=[],
                                   test_run_refs=["test-runs/1.mothership"])
    assert links == []  # identical (criterion, kind, ref) already present


def test_preserves_manual_evidence_and_only_adds():  # ac7
    spec = _spec([AcceptanceCriterion(
        id="ac1", text="x",
        evidence=[AcceptanceEvidence(kind="artifact", ref="https://manual")])])
    links = compute_evidence_links(spec, commits=[],
                                   test_run_refs=["test-runs/1.mothership"])
    assert links == [EvidenceLink("ac1", "test", "test-runs/1.mothership")]
    # the planner never mutates: the manual entry is untouched
    assert spec.acceptance_criteria[0].evidence == [
        AcceptanceEvidence(kind="artifact", ref="https://manual")]


def test_idempotent_when_links_applied_then_recomputed():  # ac6
    spec = _spec([AcceptanceCriterion(id="ac1", text="x")])
    commits = [("sha1", "implement ac1")]
    refs = ["test-runs/1.mothership"]
    first = compute_evidence_links(spec, commits, refs)
    for link in first:
        set_criterion_evidence(spec, link.criterion_id, link.kind, link.ref)
    second = compute_evidence_links(spec, commits, refs)
    assert second == []  # nothing new on the second pass
    assert sorted(e.kind for e in spec.acceptance_criteria[0].evidence) == ["commit", "test"]
