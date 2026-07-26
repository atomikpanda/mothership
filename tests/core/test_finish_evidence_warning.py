"""finish's acceptance-block wrapper: embeds image evidence when the workspace's
evidence commit is fetchable, and warns the operator (once, actionably) when it
exists but is not — because `workspace_raw_base` answers only the git question
and does not know the storage mode, the wrapper must gate on `committed` mode
before ever calling it (see evidence_url.py's module docstring): under `local`
the evidence dir is gitignored, so a URL would 404 for bytes never on the remote.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from mship.core.pr import acceptance_block_for_finish
from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, Spec

IMAGE_REF = "a1b2c3d4e5f6.png"


def _spec_with(*evidence: AcceptanceEvidence) -> Spec:
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    return Spec(
        id="my-spec", title="My spec", status="approved",
        created_at=now, updated_at=now,
        acceptance_criteria=[
            AcceptanceCriterion(
                id="ac1", text="the screen renders", verdict="approved",
                evidence=list(evidence),
            )
        ],
    )


def _config(evidence_storage=None, spec_storage="committed"):
    return SimpleNamespace(spec_storage=spec_storage, evidence_storage=evidence_storage)


class _FakeShell:
    """Stands in for util/shell.py::ShellRunner. `run` takes a command STRING.

    `replies` maps a command fragment to (returncode, stdout); first match wins,
    anything unmatched fails. Every invocation is recorded in `commands` so a
    test can assert the shell was NEVER called at all (the local/encrypted gate).
    """

    def __init__(self, replies: dict[str, tuple[int, str]] | None = None):
        self._replies = replies or {}
        self.commands: list[str] = []

    def run(self, command, cwd, env=None, timeout=None):
        self.commands.append(command)
        for fragment, (rc, out) in self._replies.items():
            if fragment in command:
                return type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()


def _pushed_shell() -> _FakeShell:
    return _FakeShell({
        "remote get-url": (0, "git@github.com:o/r.git\n"),
        "--abbrev-ref": (0, "main\n"),
        "rev-parse HEAD": (0, "abc123def456\n"),
        "ls-remote": (0, "abc123def456\trefs/heads/main\n"),
    })


def _unpushed_shell() -> _FakeShell:
    return _FakeShell({
        "remote get-url": (0, "git@github.com:o/r.git\n"),
        "--abbrev-ref": (0, "main\n"),
        "rev-parse HEAD": (0, "abc123def456\n"),
        "ls-remote": (0, ""),  # no matching line for the branch => not on origin
    })


def test_committed_and_not_pushed_names_the_artifact_and_warns(tmp_path):
    spec = _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF))
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, _unpushed_shell(), _config(),
    )
    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None
    assert "pushed" in warning.lower()
    assert "specs/" in warning


def test_committed_and_pushed_embeds_with_no_warning(tmp_path):
    spec = _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF))
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, _pushed_shell(), _config(),
    )
    assert f"![ac1](" in block
    assert IMAGE_REF in block
    assert warning is None


def test_local_mode_never_calls_the_shell_and_warns(tmp_path):
    spec = _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF))
    shell = _pushed_shell()  # would happily answer "pushed" if ever asked
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, shell, _config(evidence_storage="local"),
    )
    assert shell.commands == []
    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None


def test_encrypted_mode_names_the_artifact_and_warns(tmp_path):
    spec = _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF))
    shell = _pushed_shell()
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, shell, _config(evidence_storage="encrypted"),
    )
    assert shell.commands == []
    assert "![" not in block
    assert warning is not None


def test_no_image_evidence_produces_no_warning(tmp_path):
    """test/commit refs only — nothing embeddable, so nothing to nag about."""
    spec = _spec_with(
        AcceptanceEvidence(kind="test", ref="test-runs/7"),
        AcceptanceEvidence(kind="commit", ref="deadbee"),
    )
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, _unpushed_shell(), _config(),
    )
    assert warning is None


def test_no_evidence_at_all_produces_no_warning(tmp_path):
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    spec = Spec(
        id="my-spec", title="My spec", status="approved",
        created_at=now, updated_at=now,
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="does X")],
    )
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, _FakeShell(), _config(),
    )
    assert warning is None
