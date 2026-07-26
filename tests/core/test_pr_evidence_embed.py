"""Image evidence in the PR body: embedded when GitHub can fetch the bytes,
named when it cannot.

There is no public API for uploading an image attachment to a PR, so an embed
only works when the artifact already lives somewhere GitHub's renderer can
reach — i.e. committed to the public workspace repo and pushed. Everything here
is about the two halves of that: rendering (`build_acceptance_block`, pure) and
deciding whether a fetchable base URL exists at all (`workspace_raw_base`).
"""
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mship.core.pr import build_acceptance_block
from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, Spec

BASE = "https://raw.githubusercontent.com/o/r/abc123/specs/evidence"
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


# --- rendering -------------------------------------------------------------


def test_image_artifact_is_embedded_when_a_base_url_is_available():
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF)),
        evidence_base_url=BASE,
    )
    assert f"![ac1]({BASE}/my-spec/{IMAGE_REF})" in body


def test_without_a_base_url_the_artifact_is_named_not_embedded():
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF)),
        evidence_base_url=None,
    )
    assert "![" not in body
    assert IMAGE_REF in body


def test_non_image_artifact_is_never_embedded():
    ref = "a1b2c3d4e5f6.xml"
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref=ref)),
        evidence_base_url=BASE,
    )
    assert "![" not in body
    assert f"artifact:{ref}" in body


def test_encrypted_artifact_is_never_embedded():
    """The bytes on GitHub are ciphertext, so an embed would render broken."""
    ref = f"{IMAGE_REF}.enc"
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref=ref)),
        evidence_base_url=BASE,
    )
    assert "![" not in body
    assert f"artifact:{ref}" in body


def test_artifact_ref_that_is_not_a_stored_ref_is_never_embedded():
    """Only refs the evidence store produced resolve under the base URL; a
    hand-written path would embed a URL that 404s."""
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref="docs/shot.png")),
        evidence_base_url=BASE,
    )
    assert "![" not in body
    assert "artifact:docs/shot.png" in body


def test_test_and_commit_refs_render_as_before():
    body = build_acceptance_block(
        _spec_with(
            AcceptanceEvidence(kind="test", ref="test-runs/7"),
            AcceptanceEvidence(kind="commit", ref="deadbee"),
        ),
        evidence_base_url=BASE,
    )
    assert "- [x] `ac1` the screen renders — test:test-runs/7, commit:deadbee" in body
    assert "![" not in body


def test_embedded_image_sits_on_its_own_line_beneath_the_criterion():
    body = build_acceptance_block(
        _spec_with(
            AcceptanceEvidence(kind="test", ref="test-runs/7"),
            AcceptanceEvidence(kind="artifact", ref=IMAGE_REF),
        ),
        evidence_base_url=BASE,
    )
    lines = body.splitlines()
    i = lines.index("- [x] `ac1` the screen renders — test:test-runs/7")
    assert lines[i + 1] == ""
    assert lines[i + 2] == f"  ![ac1]({BASE}/my-spec/{IMAGE_REF})"


def test_criterion_with_no_evidence_is_unchanged():
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    spec = Spec(
        id="my-spec", title="My spec", status="approved",
        created_at=now, updated_at=now,
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="does X")],
    )
    assert "- [ ] `ac1` does X — _no evidence_" in build_acceptance_block(
        spec, evidence_base_url=BASE
    )


def test_default_call_still_names_artifacts():
    """Existing callers pass no base URL and must be unaffected."""
    body = build_acceptance_block(
        _spec_with(AcceptanceEvidence(kind="artifact", ref=IMAGE_REF))
    )
    assert "![" not in body
    assert f"artifact:{IMAGE_REF}" in body


# --- base-URL resolution ---------------------------------------------------


class _FakeShell:
    """Stands in for util/shell.py::ShellRunner. `run` takes a command STRING.

    `replies` maps a command fragment to (returncode, stdout); first match wins,
    anything unmatched fails.
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


def _pushed_shell(remote_url: str) -> _FakeShell:
    return _FakeShell({
        "remote get-url": (0, remote_url),
        "--abbrev-ref": (0, "main\n"),
        "rev-parse HEAD": (0, "abc123def456\n"),
        "ls-remote": (0, "abc123def456\trefs/heads/main\n"),
    })


def test_ssh_remote_yields_a_sha_pinned_raw_base(tmp_path):
    from mship.core.evidence_url import workspace_raw_base

    assert workspace_raw_base(
        tmp_path, _pushed_shell("git@github.com:o/r.git\n")
    ) == "https://raw.githubusercontent.com/o/r/abc123def456/specs/evidence"


def test_https_remote_yields_the_same_base(tmp_path):
    from mship.core.evidence_url import workspace_raw_base

    assert workspace_raw_base(
        tmp_path, _pushed_shell("https://github.com/o/r.git\n")
    ) == "https://raw.githubusercontent.com/o/r/abc123def456/specs/evidence"


def test_non_github_remote_has_no_base(tmp_path):
    from mship.core.evidence_url import workspace_raw_base

    assert workspace_raw_base(
        tmp_path, _pushed_shell("git@gitlab.com:o/r.git\n")
    ) is None


def test_missing_remote_has_no_base(tmp_path):
    from mship.core.evidence_url import workspace_raw_base

    assert workspace_raw_base(tmp_path, _FakeShell()) is None


def test_detached_head_has_no_base(tmp_path):
    from mship.core.evidence_url import workspace_raw_base

    shell = _FakeShell({
        "remote get-url": (0, "git@github.com:o/r.git\n"),
        "--abbrev-ref": (0, "HEAD\n"),
        "rev-parse HEAD": (0, "abc123def456\n"),
    })
    assert workspace_raw_base(tmp_path, shell) is None


def test_unreachable_remote_has_no_base(tmp_path):
    from mship.core.evidence_url import workspace_raw_base

    shell = _FakeShell({
        "remote get-url": (0, "git@github.com:o/r.git\n"),
        "--abbrev-ref": (0, "main\n"),
        "rev-parse HEAD": (0, "abc123def456\n"),
        "ls-remote": (128, ""),
    })
    assert workspace_raw_base(tmp_path, shell) is None


# --- the pushed/unpushed call, against real git ----------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _real_repo(tmp_path: Path) -> Path:
    """A working clone whose origin is a local bare repo living at a path that
    parses as a GitHub slug — so the slug is real, the git plumbing is real, and
    no network is touched."""
    origin = tmp_path / "github.com" / "o" / "r.git"
    origin.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    repo = tmp_path / "wc"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "specs").mkdir()
    (repo / "specs" / "f.md").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "evidence")
    return repo


def test_real_repo_unpushed_head_has_no_base(tmp_path):
    from mship.core.evidence_url import workspace_raw_base
    from mship.util.shell import ShellRunner

    repo = _real_repo(tmp_path)
    assert workspace_raw_base(repo, ShellRunner()) is None


def test_real_repo_pushed_head_pins_the_sha(tmp_path):
    from mship.core.evidence_url import workspace_raw_base
    from mship.util.shell import ShellRunner

    repo = _real_repo(tmp_path)
    _git(repo, "push", "-q", "-u", "origin", "main")
    sha = _git(repo, "rev-parse", "HEAD")

    assert workspace_raw_base(repo, ShellRunner()) == (
        f"https://raw.githubusercontent.com/o/r/{sha}/specs/evidence"
    )


def test_real_repo_head_behind_a_pushed_tip_is_still_fetchable(tmp_path):
    """HEAD reachable from the remote tip is on the remote — the evidence blob
    at that sha is fetchable even though the branch has moved on."""
    from mship.core.evidence_url import workspace_raw_base
    from mship.util.shell import ShellRunner

    repo = _real_repo(tmp_path)
    (repo / "specs" / "g.md").write_text("y")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "more")
    _git(repo, "push", "-q", "-u", "origin", "main")
    _git(repo, "checkout", "-q", "-B", "main", "HEAD~1")
    sha = _git(repo, "rev-parse", "HEAD")

    assert workspace_raw_base(repo, ShellRunner()) == (
        f"https://raw.githubusercontent.com/o/r/{sha}/specs/evidence"
    )


def test_real_repo_new_local_commit_after_a_push_has_no_base(tmp_path):
    from mship.core.evidence_url import workspace_raw_base
    from mship.util.shell import ShellRunner

    repo = _real_repo(tmp_path)
    _git(repo, "push", "-q", "-u", "origin", "main")
    (repo / "specs" / "h.md").write_text("z")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "unpushed evidence")

    assert workspace_raw_base(repo, ShellRunner()) is None
