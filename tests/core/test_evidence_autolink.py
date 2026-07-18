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


from mship.core.evidence_autolink import test_run_refs_for_task
from mship.core.state import Task, TestResult

# `test_run_refs_for_task` is a production helper, not a pytest test; its `test_`
# prefix would otherwise make pytest try to collect the imported symbol.
test_run_refs_for_task.__test__ = False


def _task(**kw):
    base = dict(slug="t", description="d", phase="dev",
                created_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
                affected_repos=["mothership"], branch="feat")
    base.update(kw)
    return Task(**base)


def test_test_run_refs_only_for_passing_repos():  # ac10
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    task = _task(test_iteration=3,
                 test_results={"mothership": TestResult(status="pass", at=now),
                               "web": TestResult(status="fail", at=now)})
    assert test_run_refs_for_task(task) == ["test-runs/3.mothership"]


def test_test_run_refs_empty_without_iteration():
    task = _task(test_iteration=0, test_results={})
    assert test_run_refs_for_task(task) == []


from pathlib import Path

from mship.core.evidence_autolink import commits_since_base
from mship.util.shell import ShellResult


class _FakeShell:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def run(self, cmd, cwd=None, env=None):
        self.calls.append((cmd, cwd))
        return self._result


def test_commits_since_base_parses_nul_separated_records():
    # git separates records with \x1e and the sha from the (possibly multi-line)
    # body with \x1f; a trailing newline per record is tolerated.
    stdout = "sha1\x1fimplement ac1\nmore body\x1e\nsha2\x1ffix ac2\x1e\n"
    shell = _FakeShell(ShellResult(returncode=0, stdout=stdout, stderr=""))
    commits = commits_since_base(shell, Path("/repo"), "main", "feat")
    assert commits == [("sha1", "implement ac1\nmore body"), ("sha2", "fix ac2")]
    assert "origin/main..feat" in shell.calls[0][0]


def test_commits_since_base_empty_on_git_failure():
    shell = _FakeShell(ShellResult(returncode=128, stdout="", stderr="fatal"))
    assert commits_since_base(shell, Path("/repo"), "main", "feat") == []
