"""The seam between writing an evidence artifact and linking to it, exercised
end to end against real git repos.

The bug this covers was not in either half. `capture --evidence` correctly wrote
a file; the URL builder correctly answered "is this sha on origin?". Nobody put
the bytes anywhere GitHub could serve them, and nobody checked that the sha in
the URL contained them — so the ordinary sequence emitted an image URL that
404ed, silently. Unit tests on each half would have stayed green throughout.

So everything here drives `acceptance_block_for_finish` (what `mship finish`
actually calls) against a real working clone with a real bare origin, and asserts
on both repos' state afterwards as well as on the rendered block. The origin is a
local bare repo living at a path that happens to parse as a GitHub slug: real git
plumbing, real push semantics, no network.

The target is the MEMBER REPO the pull request goes to, on an orphan
`mship-evidence` branch — never the default branch, never the workspace repo (the
store is machine-local and lives outside any repo's tree). `main` being untouched
is the load-bearing property, so it is asserted explicitly and not just implied.
"""
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mship.core.evidence_store import evidence_dir
from mship.core.evidence_url import ORPHAN_BRANCH
from mship.core.pr import acceptance_block_for_finish
from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, Spec
from mship.util.shell import ShellRunner

SPEC_ID = "my-spec"
IMAGE_REF = "a1b2c3d4e5f6.png"
TREE_PATH = f"{SPEC_ID}/{IMAGE_REF}"
PNG_BYTES = b"\x89PNG\r\n\x1a\n-not-really-a-png-but-git-does-not-care"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _git_ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True).returncode == 0


def _status(repo: Path) -> str:
    """`git status --porcelain -uall`, UNstripped — the leading column is the
    staged/unstaged distinction several of these tests are about."""
    return subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout


def _spec(*refs: str) -> Spec:
    now = datetime(2026, 7, 26, tzinfo=timezone.utc)
    return Spec(
        id=SPEC_ID, title="My spec", status="approved",
        created_at=now, updated_at=now,
        acceptance_criteria=[
            AcceptanceCriterion(
                id=f"ac{i}", text="the screen renders", verdict="approved",
                evidence=[AcceptanceEvidence(kind="artifact", ref=ref)],
            )
            for i, ref in enumerate(refs or (IMAGE_REF,), start=1)
        ],
    )


def _config(evidence_storage=None, spec_storage="committed"):
    return SimpleNamespace(spec_storage=spec_storage, evidence_storage=evidence_storage)


class _SpyShell(ShellRunner):
    """A real ShellRunner that remembers every command, so a test can assert no
    git work happened at all."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command, cwd, env=None, timeout=None):
        self.commands.append(command)
        return super().run(command, cwd, env=env, timeout=timeout)


def _init_origin(tmp_path: Path, name: str = "r") -> Path:
    bare = tmp_path / "github.com" / "o" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    return bare


def _init_member(tmp_path: Path, origin: Path, name: str = "member") -> Path:
    """A member repo with one pushed commit on `main` — the repo a PR targets."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "product.py").write_text("the product's own code\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "product")
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _store(workspace: Path, ref: str = IMAGE_REF, body: bytes = PNG_BYTES) -> Path:
    """Seed the machine-local evidence store exactly as `capture --evidence`
    leaves it. Located via evidence_store so this says WHAT, not where."""
    path = evidence_dir(workspace, SPEC_ID) / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return _init_origin(tmp_path)


@pytest.fixture
def member(tmp_path: Path, origin: Path) -> Path:
    return _init_member(tmp_path, origin)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """The workspace root holding the local evidence store. Deliberately NOT a
    git repo: nothing in publication may depend on it being one."""
    root = tmp_path / "workspace"
    root.mkdir()
    _store(root)
    return root


def _finish(workspace: Path, repo: Path, spec=None, config=None, shell=None):
    return acceptance_block_for_finish(
        spec or _spec(), workspace, repo, shell or ShellRunner(), config or _config(),
    )


def _embedded_url(block: str) -> str | None:
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("!["):
            return stripped[stripped.index("(") + 1: -1]
    return None


def _sha_from(url: str) -> str:
    # .../<owner>/<repo>/<sha>/<spec-id>/<ref>
    return url.split("/")[-3]


def _blob_at(repo: Path, sha: str, tree_path: str) -> bytes:
    return subprocess.run(
        ["git", "cat-file", "-p", f"{sha}:{tree_path}"],
        cwd=repo, capture_output=True, check=True,
    ).stdout


def _assert_url_resolves(origin: Path, url: str, tree_path: str = TREE_PATH) -> str:
    """The URL is not merely well-formed: the sha it pins is on the REMOTE and
    carries the artifact's bytes. Together that is exactly what
    raw.githubusercontent.com needs to return an image instead of a 404.

    Asserted against the bare origin, not the local clone — a local-only commit
    is the one wrong answer here.
    """
    sha = _sha_from(url)
    assert url.endswith(f"/{sha}/{tree_path}"), url
    assert _blob_at(origin, sha, tree_path) == PNG_BYTES, "the remote's bytes differ"
    return sha


def _main_shas(repo: Path, origin: Path) -> tuple[str, str]:
    return _git(repo, "rev-parse", "main"), _git(origin, "rev-parse", "main")


# --- the ordinary path -------------------------------------------------------


def test_first_publication_creates_the_orphan_branch_and_embeds(workspace, member, origin):
    """The ordinary sequence: `capture --evidence`, then `finish`. No evidence
    branch exists yet, so this is also the branch-creation case."""
    block, warning = _finish(workspace, member)

    assert warning is None
    url = _embedded_url(block)
    assert url is not None, f"expected an embed, got:\n{block}"
    assert url.startswith("https://raw.githubusercontent.com/o/r/")
    _assert_url_resolves(origin, url)
    assert _git(origin, "rev-parse", ORPHAN_BRANCH)


def test_main_is_never_touched_locally_or_on_the_origin(workspace, member, origin):
    """ac6, and the whole point of the rework. Publishing must not create a
    commit on the default branch, move it, or push it — in either repo."""
    before = _main_shas(member, origin)
    head_before = _git(member, "rev-parse", "HEAD")
    branch_before = _git(member, "rev-parse", "--abbrev-ref", "HEAD")
    status_before = _status(member)

    block, warning = _finish(workspace, member)

    assert _embedded_url(block) is not None and warning is None
    assert _main_shas(member, origin) == before, "the default branch moved"
    assert _git(member, "rev-parse", "HEAD") == head_before
    assert _git(member, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
    assert _status(member) == status_before, "the working tree or index changed"
    # And no local branch was created either: the commit is pushed by sha.
    assert ORPHAN_BRANCH not in _git(member, "branch", "--list")


def test_the_orphan_branch_shares_no_history_with_main(workspace, member, origin):
    block, _ = _finish(workspace, member)
    sha = _sha_from(_embedded_url(block))

    assert not _git_ok(member, "merge-base", "--is-ancestor", sha, "main")
    assert not _git_ok(member, "merge-base", "--is-ancestor", "main", sha)
    assert not _git_ok(origin, "merge-base", "--is-ancestor", ORPHAN_BRANCH, "main")
    # Nothing of the product is in that tree, and nothing of the evidence is in main's.
    assert _git(origin, "ls-tree", "-r", "--name-only", ORPHAN_BRANCH) == TREE_PATH
    assert TREE_PATH not in _git(origin, "ls-tree", "-r", "--name-only", "main")


def test_a_second_publication_accumulates_rather_than_replacing(workspace, member, origin):
    """Published artifacts pile up on the branch: a later PR's screenshots must
    not silently drop an earlier one's."""
    _finish(workspace, member)
    first = _git(origin, "rev-parse", ORPHAN_BRANCH)

    second_ref = "b1b2c3d4e5f6.png"
    _store(workspace, second_ref)
    block, warning = _finish(workspace, member, spec=_spec(IMAGE_REF, second_ref))

    assert warning is None
    tip = _git(origin, "rev-parse", ORPHAN_BRANCH)
    assert tip != first
    assert _git(origin, "rev-parse", f"{tip}^") == first, "the new commit lost its parent"
    listed = _git(origin, "ls-tree", "-r", "--name-only", tip).split()
    assert listed == [TREE_PATH, f"{SPEC_ID}/{second_ref}"]


def test_republishing_identical_bytes_makes_no_new_commit(workspace, member, origin):
    """Refs are content hashes, so a re-run of finish publishes the same tree.
    Stacking an empty commit each time would be noise on a shared branch."""
    _finish(workspace, member)
    first = _git(origin, "rev-parse", ORPHAN_BRANCH)

    block, warning = _finish(workspace, member)

    assert warning is None
    assert _git(origin, "rev-parse", ORPHAN_BRANCH) == first
    assert _sha_from(_embedded_url(block)) == first


def test_bytes_deleted_from_the_store_still_embed_once_published(workspace, member, origin):
    """The store is machine-local and nothing stops an operator clearing it. What
    is already on the branch stays embeddable."""
    _finish(workspace, member)
    (evidence_dir(workspace, SPEC_ID) / IMAGE_REF).unlink()

    block, warning = _finish(workspace, member)

    assert warning is None
    _assert_url_resolves(origin, _embedded_url(block))


def test_the_evidence_commit_is_self_explanatory(workspace, member, origin):
    """This commit lands in the operator's own repo, so it has to say what it is
    and who made it without them having to go digging."""
    _finish(workspace, member)

    subject = _git(origin, "log", "-1", "--format=%s", ORPHAN_BRANCH)
    body = _git(origin, "log", "-1", "--format=%b", ORPHAN_BRANCH)
    assert subject == f"chore(evidence): publish 1 artifact for {SPEC_ID}"
    assert "mship finish" in body
    assert ORPHAN_BRANCH in body
    # The operator's own identity, as `mship commit` also uses — no impostor author.
    assert _git(origin, "log", "-1", "--format=%an <%ae>", ORPHAN_BRANCH) == "t <t@example.com>"


def test_a_detached_head_repo_publishes_anyway(workspace, member, origin):
    """Publication never consults HEAD — it builds the commit with plumbing — so
    a worktree parked on a detached HEAD is not a reason to degrade."""
    _git(member, "checkout", "-q", "--detach")
    before = _git(member, "rev-parse", "HEAD")

    block, warning = _finish(workspace, member)

    assert warning is None
    _assert_url_resolves(origin, _embedded_url(block))
    assert _git(member, "rev-parse", "HEAD") == before


# --- the narrowness guarantee ------------------------------------------------


def test_unrelated_uncommitted_work_is_never_swept_in(workspace, member, origin):
    """The one that protects the licence. finish publishes the referenced
    artifacts and NOTHING else — not the operator's untracked notes, not their
    edits in flight, not even work they had already staged themselves."""
    (member / "untracked-notes.md").write_text("private thinking\n")
    (member / "product.py").write_text("edited in flight\n")
    (member / "staged-by-operator.md").write_text("half-done\n")
    _git(member, "add", "--", "staged-by-operator.md")

    _finish(workspace, member)

    assert _git(origin, "ls-tree", "-r", "--name-only", ORPHAN_BRANCH) == TREE_PATH
    status = _status(member)
    assert "?? untracked-notes.md" in status, "the operator's untracked file moved"
    assert " M product.py" in status, "the operator's edit was committed"
    assert "A  staged-by-operator.md" in status, "the operator's staged work moved"


def test_local_mode_touches_neither_the_shell_nor_the_repo(workspace, member, origin):
    """Under `local` nothing may leave the machine, so the mode gate must
    short-circuit before any git call at all."""
    before = _main_shas(member, origin)
    shell = _SpyShell()

    block, warning = _finish(
        workspace, member, config=_config(evidence_storage="local"), shell=shell,
    )

    assert shell.commands == []
    assert _main_shas(member, origin) == before
    assert _git(origin, "branch", "--list") == "* main"
    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None


# --- everything that can go wrong -------------------------------------------


def test_a_rejected_push_warns_names_the_artifact_and_still_returns_a_block(
    workspace, member, origin,
):
    """The remote refuses the push. finish must not force it, must not raise, and
    must not put a URL in the body."""
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    before = _main_shas(member, origin)

    block, warning = _finish(workspace, member)

    assert "![" not in block
    assert IMAGE_REF in block, "the artifact must still be NAMED"
    assert warning is not None
    assert "push" in warning.lower()
    assert _git(origin, "branch", "--list") == "* main"
    assert _main_shas(member, origin) == before


def test_an_unreachable_origin_warns_and_never_blocks(workspace, member, origin):
    subprocess.run(["rm", "-rf", str(origin)], check=True)

    block, warning = _finish(workspace, member)

    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None


def test_a_repo_without_a_github_origin_does_no_git_writing(workspace, tmp_path):
    """No raw host to serve from means no embed is possible, so nothing is
    published — writing to someone's repo has to buy them something."""
    repo = tmp_path / "no-remote"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "x.py").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "seed")
    before = _git(repo, "rev-parse", "HEAD")

    block, warning = _finish(workspace, repo)

    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "branch", "--list") == "* main"
    assert "![" not in block
    assert warning is not None


def test_one_unpublished_artifact_does_not_drag_its_siblings_down(
    workspace, member, origin,
):
    """Verification is per-ref, so a criterion whose artifact never made it is
    named while the criterion next to it still shows its screenshot."""
    ghost = "0f0f0f0f0f0f.png"

    block, warning = _finish(workspace, member, spec=_spec(IMAGE_REF, ghost))

    assert "![ac1](" in block
    assert "![ac2](" not in block
    assert f"artifact:{ghost}" in block
    assert warning is not None
    assert "1 of 2" in warning
    _assert_url_resolves(origin, _embedded_url(block))


def test_a_missing_artifact_file_is_named_not_embedded(workspace, member):
    """The spec references a ref whose bytes are not in the store and never were,
    and nothing is on the branch to fall back to."""
    (evidence_dir(workspace, SPEC_ID) / IMAGE_REF).unlink()

    block, warning = _finish(workspace, member)

    assert "![" not in block
    assert IMAGE_REF in block
    assert warning is not None


def test_the_push_cannot_prompt_for_credentials(monkeypatch, workspace, member):
    """finish must never go interactive: a repo whose credentials are not cached
    has to fail fast, not stop mid-finish waiting on a password."""
    seen: dict[str, dict] = {}
    real = ShellRunner.run

    def spy(self, command, cwd, env=None, timeout=None):
        if command.startswith("git push"):
            seen["env"] = env or {}
            seen["timeout"] = timeout
        return real(self, command, cwd, env=env, timeout=timeout)

    monkeypatch.setattr(ShellRunner, "run", spy)
    _finish(workspace, member)

    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in seen["env"]["GIT_SSH_COMMAND"]
    assert seen["timeout"] is not None


# --- multi-repo (ac7) --------------------------------------------------------


def test_each_repo_receiving_a_pr_publishes_to_its_own_branch(workspace, tmp_path):
    """A spec spanning two repos gives each PR its own copy, so a reviewer of one
    repo's PR needs no read access to the sibling."""
    origin_a = _init_origin(tmp_path, "a")
    origin_b = _init_origin(tmp_path, "b")
    repo_a = _init_member(tmp_path, origin_a, "member-a")
    repo_b = _init_member(tmp_path, origin_b, "member-b")

    url_a = _embedded_url(_finish(workspace, repo_a)[0])
    url_b = _embedded_url(_finish(workspace, repo_b)[0])

    assert url_a.startswith("https://raw.githubusercontent.com/o/a/")
    assert url_b.startswith("https://raw.githubusercontent.com/o/b/")
    _assert_url_resolves(origin_a, url_a)
    _assert_url_resolves(origin_b, url_b)
