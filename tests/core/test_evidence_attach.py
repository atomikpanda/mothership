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
    """Stands in for util/shell.py::Shell. Its `run` takes a command STRING."""

    def __init__(self, sha_out: str, status_out: str):
        self._sha_out = sha_out
        self._status_out = status_out
        self.commands: list[str] = []

    def run(self, command, cwd, env=None, timeout=None):
        self.commands.append(command)
        out = self._sha_out if "rev-parse" in command else self._status_out
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
