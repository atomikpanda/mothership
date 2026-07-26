import pytest

from mship.core.evidence_attach import EvidenceTarget, parse_evidence_target


def test_parses_spec_and_criterion():
    t = parse_evidence_target("my-spec:ac3")
    assert t == EvidenceTarget(spec_id="my-spec", criterion_id="ac3")


@pytest.mark.parametrize("bad", ["nocolon", ":ac1", "spec:", "a:b:c", ""])
def test_rejects_malformed_targets(bad):
    with pytest.raises(ValueError):
        parse_evidence_target(bad)


class _FakeShell:
    """Stands in for util/shell.py::Shell. Its `run` takes a command STRING.

    `branch_out` defaults to a real branch name — "on a branch" is the common
    case, so tests that don't care about branch-containment don't have to
    supply it.
    """

    def __init__(self, sha_out: str, status_out: str, branch_out: str = "* main\n"):
        self._sha_out = sha_out
        self._status_out = status_out
        self._branch_out = branch_out
        self.commands: list[str] = []

    def run(self, command, cwd, env=None, timeout=None):
        self.commands.append(command)
        if "rev-parse" in command:
            out = self._sha_out
        elif "branch" in command:
            out = self._branch_out
        else:
            out = self._status_out
        return type("R", (), {"stdout": out, "stderr": "", "returncode": 0})()


def test_provenance_note_names_the_revision(tmp_path):
    from mship.core.evidence_attach import provenance_note

    note = provenance_note(tmp_path, _FakeShell("abc1234\n", ""))
    assert note == "at abc1234"


def test_provenance_note_marks_an_uncommitted_tree(tmp_path):
    from mship.core.evidence_attach import provenance_note

    note = provenance_note(tmp_path, _FakeShell("abc1234\n", " M src/foo.py\n"))
    assert "abc1234" in note and "uncommitted" in note


def test_provenance_note_survives_an_unknown_revision(tmp_path):
    from mship.core.evidence_attach import provenance_note

    assert "unknown" in provenance_note(tmp_path, _FakeShell("", ""))


def test_provenance_note_marks_a_detached_commit_not_on_any_branch(tmp_path):
    """`git branch --all --contains <sha>` still emits a synthetic
    `* (HEAD detached ...)` line even when no real branch contains the
    commit — that line must NOT be mistaken for "on a branch"."""
    from mship.core.evidence_attach import provenance_note

    note = provenance_note(
        tmp_path,
        _FakeShell("abc1234\n", "", branch_out="* (HEAD detached from 1234abc)\n"),
    )
    assert "abc1234" in note
    assert "not on any branch" in note
    assert "uncommitted" not in note  # clean tree — only the branch marker fires


def test_provenance_note_on_a_remote_only_branch_is_not_marked(tmp_path):
    """A detached checkout of a commit that IS a remote branch's tip is still
    "on a branch" — `--all` must see remote-tracking branches, not just local
    ones."""
    from mship.core.evidence_attach import provenance_note

    note = provenance_note(
        tmp_path,
        _FakeShell(
            "abc1234\n", "",
            branch_out="* (HEAD detached from 1234abc)\n  remotes/origin/main\n",
        ),
    )
    assert note == "at abc1234"


def test_provenance_note_marks_both_dirty_and_detached(tmp_path):
    from mship.core.evidence_attach import provenance_note

    note = provenance_note(
        tmp_path,
        _FakeShell("abc1234\n", " M src/foo.py\n", branch_out="* (HEAD detached from x)\n"),
    )
    assert "uncommitted working tree" in note
    assert "not on any branch" in note


def test_provenance_note_unknown_revision_skips_branch_check(tmp_path):
    """No SHA to check containment for — `git branch --contains` isn't even
    called, so it can't spuriously add "not on any branch" to an already
    unresolvable revision."""
    from mship.core.evidence_attach import provenance_note

    shell = _FakeShell("", "")
    note = provenance_note(tmp_path, shell)
    assert note == "at unknown"
    assert not any("branch" in c for c in shell.commands)
