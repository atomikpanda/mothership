"""The seam between writing an evidence artifact and linking to it, exercised
end to end against a real git repo.

The bug this covers was not in either half. `capture --evidence` correctly wrote
a file; `workspace_raw_base` correctly answered "is HEAD on origin?". Nobody
committed the file, and nobody checked that the sha in the URL contained it — so
the ordinary sequence emitted an image URL that 404ed, silently. Unit tests on
each half would have stayed green throughout. So everything here drives
`acceptance_block_for_finish` (what `mship finish` actually calls) against a real
working clone with a real origin, and asserts on the repo's state afterwards as
well as on the rendered block.

The origin is a local bare repo living at a path that happens to parse as a
GitHub slug: real git plumbing, real push semantics, no network.
"""
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mship.core.pr import acceptance_block_for_finish
from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, Spec
from mship.util.shell import ShellRunner

SPEC_ID = "my-spec"
IMAGE_REF = "a1b2c3d4e5f6.png"
RELPATH = f"specs/evidence/{SPEC_ID}/{IMAGE_REF}"
PNG_BYTES = b"\x89PNG\r\n\x1a\n-not-really-a-png-but-git-does-not-care"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _status(repo: Path) -> str:
    """`git status --porcelain -uall`, UNstripped — the leading column is the
    staged/unstaged distinction these tests are entirely about."""
    return subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout


def _spec() -> Spec:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    return Spec(
        id=SPEC_ID, title="My spec", status="approved",
        created_at=now, updated_at=now,
        acceptance_criteria=[
            AcceptanceCriterion(
                id="ac1", text="the screen renders", verdict="approved",
                evidence=[AcceptanceEvidence(kind="artifact", ref=IMAGE_REF)],
            )
        ],
    )


def _config(evidence_storage=None, spec_storage="committed"):
    return SimpleNamespace(spec_storage=spec_storage, evidence_storage=evidence_storage)


class _SpyShell(ShellRunner):
    """A real ShellRunner that remembers every command, so a test can assert the
    workspace repo was never touched at all."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command, cwd, env=None, timeout=None):
        self.commands.append(command)
        return super().run(command, cwd, env=env, timeout=timeout)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "github.com" / "o" / "r.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    return bare


@pytest.fixture
def workspace(tmp_path: Path, origin: Path) -> Path:
    """A workspace clone with one pushed commit, an evidence artifact sitting
    UNTRACKED on disk exactly as `capture --evidence` leaves it, and the
    operator's own prose already committed."""
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "specs").mkdir()
    (repo / "specs" / f"{SPEC_ID}.md").write_text("the operator's prose\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "spec")
    _git(repo, "push", "-q", "-u", "origin", "main")

    artifact = repo / RELPATH
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(PNG_BYTES)
    return repo


def _finish(repo: Path, config=None, shell=None) -> tuple[str, str | None]:
    return acceptance_block_for_finish(
        _spec(), repo, shell or ShellRunner(), config or _config(),
    )


def _embedded_url(block: str) -> str | None:
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("!["):
            return stripped[stripped.index("(") + 1: -1]
    return None


def _sha_from(url: str) -> str:
    # .../<owner>/<repo>/<sha>/specs/evidence/<spec>/<ref>
    return url.split("/")[-5]


def _assert_url_resolves(repo: Path, origin: Path, url: str) -> None:
    """The URL is not merely well-formed: its sha contains the artifact, and
    that sha is on the remote. Together that is exactly what
    raw.githubusercontent.com needs to return bytes instead of a 404."""
    sha = _sha_from(url)
    assert url.endswith(f"/{sha}/{RELPATH}")
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}:{RELPATH}"], cwd=repo
        ).returncode == 0
    ), "the pinned sha does not contain the artifact"
    # Ask the remote, not the local clone: the bytes must be fetchable by GitHub.
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{sha}:{RELPATH}"], cwd=origin
    ).returncode == 0, "the pinned sha is not on the remote"


# --- the three states an artifact can be in when finish runs ---------------


def test_never_committed_artifact_is_published_and_embedded(workspace, origin):
    """The ordinary sequence: `capture --evidence`, then `finish` without ever
    touching `specs/`. This is the case that used to emit a 404."""
    block, warning = _finish(workspace)

    assert warning is None
    url = _embedded_url(block)
    assert url is not None, f"expected an embed, got:\n{block}"
    _assert_url_resolves(workspace, origin, url)


def test_committed_but_unpushed_artifact_is_pushed_and_embedded(workspace, origin):
    _git(workspace, "add", "--", RELPATH)
    _git(workspace, "commit", "-qm", "evidence, by hand")

    block, warning = _finish(workspace)

    assert warning is None
    url = _embedded_url(block)
    assert url is not None, f"expected an embed, got:\n{block}"
    _assert_url_resolves(workspace, origin, url)


def test_already_tracked_and_pushed_embeds_without_a_new_commit(workspace, origin):
    _git(workspace, "add", "--", RELPATH)
    _git(workspace, "commit", "-qm", "evidence, by hand")
    _git(workspace, "push", "-q", "origin", "main")
    before = _git(workspace, "rev-parse", "HEAD")

    block, warning = _finish(workspace)

    assert warning is None
    assert _git(workspace, "rev-parse", "HEAD") == before, "made a pointless commit"
    url = _embedded_url(block)
    assert url is not None
    _assert_url_resolves(workspace, origin, url)


# --- the narrowness guarantee ----------------------------------------------


def test_unrelated_uncommitted_work_is_never_swept_in(workspace):
    """The one that protects the licence. finish writes to the operator's own
    workspace repo, so it must commit the artifact and NOTHING else — not their
    untracked notes, not their edits in flight, not even work they had already
    staged themselves."""
    (workspace / "untracked-notes.md").write_text("private thinking\n")
    (workspace / "specs" / f"{SPEC_ID}.md").write_text("the operator's EDITED prose\n")
    (workspace / "staged-by-operator.md").write_text("half-done\n")
    _git(workspace, "add", "--", "staged-by-operator.md")

    _finish(workspace)

    committed = _git(workspace, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == [RELPATH]

    status = _status(workspace)
    assert "?? untracked-notes.md" in status, "the operator's untracked file moved"
    assert f" M specs/{SPEC_ID}.md" in status, "the operator's edit was committed"
    assert "A  staged-by-operator.md" in status, "the operator's staged work moved"


def test_the_evidence_commit_is_scoped_and_self_explanatory(workspace):
    """This commit lands in the operator's own history, so it has to say what it
    is and who made it without them having to go digging."""
    _finish(workspace)

    subject = _git(workspace, "log", "-1", "--format=%s")
    body = _git(workspace, "log", "-1", "--format=%b")
    assert subject == f"chore(evidence): publish 1 artifact for {SPEC_ID}"
    assert "mship finish" in body
    assert f"specs/evidence/{SPEC_ID}/" in body
    # The operator's own identity, as `mship commit` also uses — no impostor author.
    assert _git(workspace, "log", "-1", "--format=%an <%ae>") == "t <t@example.com>"


# --- local storage: nothing at all ------------------------------------------


def test_local_mode_touches_neither_the_shell_nor_the_repo(workspace):
    """Under `local` the evidence dir is gitignored, so staging it would be
    actively wrong — and the mode gate must short-circuit before any git call."""
    before = _git(workspace, "rev-parse", "HEAD")
    shell = _SpyShell()

    block, warning = _finish(workspace, _config(evidence_storage="local"), shell)

    assert shell.commands == []
    assert _git(workspace, "rev-parse", "HEAD") == before
    assert RELPATH in _status(workspace)
    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None


# --- everything that can go wrong with the push -----------------------------


def test_a_rejected_push_warns_names_the_artifact_and_still_returns_a_block(
    workspace, origin, tmp_path,
):
    """Origin moved on underneath us, so the push is rejected. finish must not
    force it, must not raise, and must not put a URL in the body."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "o")
    _git(other, "config", "commit.gpgsign", "false")
    (other / "elsewhere.md").write_text("someone else's commit\n")
    _git(other, "add", ".")
    _git(other, "commit", "-qm", "diverge")
    _git(other, "push", "-q", "origin", "main")

    block, warning = _finish(workspace)

    assert "![" not in block
    assert IMAGE_REF in block, "the artifact must still be NAMED"
    assert warning is not None
    assert "push" in warning.lower()
    # Not forced: origin still points at the other clone's commit.
    assert _git(origin, "rev-parse", "main") == _git(other, "rev-parse", "HEAD")


def test_an_unreachable_origin_warns_and_never_blocks(workspace, origin):
    subprocess.run(["rm", "-rf", str(origin)], check=True)

    block, warning = _finish(workspace)

    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None


def test_an_origin_without_our_branch_is_never_created_by_finish(tmp_path, origin):
    """A workspace whose branch has never been pushed stays unpushed: publishing
    the operator's whole specs/config repo is far beyond the licence to commit
    one screenshot."""
    repo = tmp_path / "never-pushed"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "seed.md").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed")
    artifact = repo / RELPATH
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(PNG_BYTES)

    block, warning = _finish(repo)

    assert _git(origin, "branch", "--list") == "", "finish created a branch on origin"
    assert "![" not in block
    assert warning is not None


def test_a_detached_head_workspace_commits_nothing(workspace):
    """A commit here would be unreachable from any branch, and the operator's
    next checkout would delete the artifact along with it."""
    _git(workspace, "checkout", "-q", "--detach")
    before = _git(workspace, "rev-parse", "HEAD")

    block, warning = _finish(workspace)

    assert _git(workspace, "rev-parse", "HEAD") == before
    assert RELPATH in _status(workspace)
    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None
    assert "detached" in warning


def test_a_gitignored_evidence_dir_is_never_forced_past(workspace):
    """Left over from a spell under `local` storage. `git add` without `-f`
    refuses, and we take the refusal rather than overriding the operator."""
    (workspace / ".gitignore").write_text("specs/evidence/\n")
    _git(workspace, "add", "--", ".gitignore")
    _git(workspace, "commit", "-qm", "ignore evidence")
    _git(workspace, "push", "-q", "origin", "main")

    block, warning = _finish(workspace)

    assert RELPATH not in _status(workspace)
    assert "![" not in block
    assert warning is not None


def test_one_unpublished_artifact_does_not_drag_its_siblings_down(workspace, origin):
    """Verification is per-ref, so a criterion whose artifact never made it is
    named while the criterion next to it still shows its screenshot."""
    ghost = "0f0f0f0f0f0f.png"
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    spec = Spec(
        id=SPEC_ID, title="My spec", status="approved",
        created_at=now, updated_at=now,
        acceptance_criteria=[
            AcceptanceCriterion(
                id="ac1", text="the screen renders", verdict="approved",
                evidence=[AcceptanceEvidence(kind="artifact", ref=IMAGE_REF)],
            ),
            AcceptanceCriterion(
                id="ac2", text="the other screen renders", verdict="approved",
                evidence=[AcceptanceEvidence(kind="artifact", ref=ghost)],
            ),
        ],
    )

    block, warning = acceptance_block_for_finish(
        spec, workspace, ShellRunner(), _config(),
    )

    assert f"![ac1](" in block
    assert "![ac2](" not in block
    assert f"artifact:{ghost}" in block
    assert warning is not None
    assert "1 of 2" in warning
    _assert_url_resolves(workspace, origin, _embedded_url(block))


def test_a_missing_artifact_file_is_named_not_embedded(workspace):
    """The spec references a ref whose bytes are not on disk and never were."""
    (workspace / RELPATH).unlink()

    block, warning = _finish(workspace)

    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None
    assert "not tracked at workspace commit" in warning


def test_the_push_cannot_prompt_for_credentials(monkeypatch, workspace):
    """finish must never go interactive: a workspace repo whose credentials are
    not cached has to fail fast, not stop mid-finish waiting on a password."""
    seen: dict[str, dict] = {}
    real = ShellRunner.run

    def spy(self, command, cwd, env=None, timeout=None):
        if command.startswith("git push"):
            seen["env"] = env or {}
            seen["timeout"] = timeout
        return real(self, command, cwd, env=env, timeout=timeout)

    monkeypatch.setattr(ShellRunner, "run", spy)
    _finish(workspace)

    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in seen["env"]["GIT_SSH_COMMAND"]
    assert seen["timeout"] is not None
