"""finish's acceptance-block wrapper: embeds image evidence when the artifact is
provably present at a published commit, and warns the operator (once, actionably)
when it is not.

Because `publish_evidence` answers only the git question and does not know the
storage mode, the wrapper must gate on `committed` mode before ever calling it
(see evidence_url.py's module docstring): under `local` nothing may leave the
machine and under `encrypted` the bytes are ciphertext, so publishing either
would be actively wrong. The gate is asserted here as "the shell was never
invoked at all".

These are the wrapper's decisions with a scripted shell. The publication seam
itself is covered end-to-end against real git repos in
test_finish_evidence_publish.py.
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


TIP = "abc123def456"


def _published_shell() -> _FakeShell:
    """An evidence branch already carrying this artifact: the store holds no file
    (tmp_path is empty), so nothing new is built and the existing tip is pinned.

    Fragment order matters — the commit-readable probe is matched before the
    generic `cat-file` reply so a test can flip one without the other.
    """
    return _FakeShell({
        "remote get-url": (0, "git@github.com:o/r.git\n"),
        "ls-remote": (0, f"{TIP}\trefs/heads/mship-evidence\n"),
        "^{commit}": (0, ""),  # the tip's objects are readable here
        "cat-file": (0, ""),   # the artifact IS in the pinned commit's tree
    })


def _unpublished_shell() -> _FakeShell:
    return _FakeShell({
        "remote get-url": (0, "git@github.com:o/r.git\n"),
        "ls-remote": (0, ""),  # the evidence branch does not exist yet
    })


def test_nothing_published_names_the_artifact_and_warns(tmp_path):
    spec = _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF))
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, tmp_path, _unpublished_shell(), _config(),
    )
    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None
    assert "mship-evidence" in warning


def test_published_and_present_embeds_with_no_warning(tmp_path):
    spec = _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF))
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, tmp_path, _published_shell(), _config(),
    )
    assert f"![ac1](" in block
    assert IMAGE_REF in block
    assert warning is None


def test_pushed_but_not_tracked_at_the_pinned_sha_is_named_not_embedded(tmp_path):
    """The seatbelt: the pinned commit is on origin, but the artifact is not in
    its tree, so the URL would 404. The module emitting the URL checks its own
    precondition instead of trusting whoever was supposed to publish the file."""
    shell = _published_shell()
    shell._replies["cat-file"] = (128, "")
    spec = _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF))
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, tmp_path, shell, _config(),
    )
    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None
    assert "not present at evidence commit" in warning


def test_local_mode_never_calls_the_shell_and_warns(tmp_path):
    spec = _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF))
    shell = _published_shell()  # would happily answer "published" if ever asked
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, tmp_path, shell, _config(evidence_storage="local"),
    )
    assert shell.commands == []
    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None


def test_encrypted_mode_names_the_artifact_and_warns(tmp_path):
    spec = _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF))
    shell = _published_shell()
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, tmp_path, shell, _config(evidence_storage="encrypted"),
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
    shell = _unpublished_shell()
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, tmp_path, shell, _config(),
    )
    assert warning is None
    # Nothing embeddable => no reason to touch any repo.
    assert shell.commands == []


def test_no_evidence_at_all_produces_no_warning(tmp_path):
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    spec = Spec(
        id="my-spec", title="My spec", status="approved",
        created_at=now, updated_at=now,
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="does X")],
    )
    block, warning = acceptance_block_for_finish(
        spec, tmp_path, tmp_path, _FakeShell(), _config(),
    )
    assert warning is None
