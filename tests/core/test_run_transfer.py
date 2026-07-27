"""Commit synthesis: a real commit object whose tree is the working tree, built
without touching the operator's repository.

Real git, real repositories, real ShellRunner throughout. The spec's own risk
list says the failure here is SILENT — an empty temporary index drops
tracked-and-gitignored files with no error — so nothing in this file is allowed
to be asserted against a mock.
"""
import os
import subprocess
from pathlib import Path

import pytest

from mship.core.run_transfer import RunTransferError, synthesize_commit
from mship.util.shell import ShellRunner


def _git_env() -> dict[str, str]:
    """Keep the operator's global git config out of these repos, exactly as
    tests/core/test_git_receive.py:195 does and for the same reason.

    Built per call, not snapshotted at import: `tests/conftest.py` installs its
    GIT_CONFIG_* signing overrides in a session fixture that runs AFTER this
    module is imported, and two tests below monkeypatch os.environ. A snapshot
    would silently hand git a different environment than the one under test.
    The identity is pinned unconditionally so the helper keeps working in the
    test that strips identity from os.environ.
    """
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env=_git_env(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo on a feature branch with one commit, a .gitignore, and a
    deliberately ignored file."""
    path = tmp_path / "api"
    path.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=path)
    (path / "a.txt").write_text("one\n")
    (path / ".gitignore").write_text("ignored.txt\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    _git("checkout", "-q", "-b", "feat/x", cwd=path)
    return path


def _head(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo)


def _dirty(repo: Path) -> None:
    (repo / "a.txt").write_text("one\nedited\n")
    (repo / "untracked.txt").write_text("scratch\n")
    (repo / "ignored.txt").write_text("secret\n")


def _tree_files(repo: Path, sha: str) -> list[str]:
    return _git("ls-tree", "-r", "--name-only", sha, cwd=repo).splitlines()


def test_the_synthesized_tree_is_the_working_tree(repo):
    """ac1: what the run host is asked to materialize contains the modified file
    exactly as it is on disk."""
    _dirty(repo)
    sha = synthesize_commit(ShellRunner(), repo, base_sha=_head(repo))

    blob = _git("show", f"{sha}:a.txt", cwd=repo)
    assert blob == "one\nedited"
    assert (repo / "a.txt").read_text() == blob + "\n"


def test_untracked_files_travel_and_gitignored_ones_do_not(repo):
    """ac11, pinned in both directions."""
    _dirty(repo)
    files = _tree_files(repo, synthesize_commit(ShellRunner(), repo, base_sha=_head(repo)))
    assert "untracked.txt" in files
    assert "ignored.txt" not in files


def test_a_tracked_file_that_is_also_gitignored_survives(repo):
    """ac1's second half, and the spec's top risk. Verified failure mode: with
    an EMPTY temporary index `git add -A` silently skips this file, because git
    will not add an ignored path that is not already in the index — the run host
    would then execute a tree missing a file the operator can plainly see.
    Seeding the scratch index from the base commit is what keeps it."""
    (repo / ".gitignore").write_text("ignored.txt\na.txt\n")
    files = _tree_files(repo, synthesize_commit(ShellRunner(), repo, base_sha=_head(repo)))
    assert "a.txt" in files


def test_a_modified_tracked_and_gitignored_file_carries_its_WORKING_content(repo):
    """Seeding from the base commit must not mean shipping the base commit's
    version: it seeds, then `git add -A` overwrites from the working tree."""
    (repo / ".gitignore").write_text("a.txt\n")
    (repo / "a.txt").write_text("edited while ignored\n")
    sha = synthesize_commit(ShellRunner(), repo, base_sha=_head(repo))
    assert _git("show", f"{sha}:a.txt", cwd=repo) == "edited while ignored"


def test_a_deleted_tracked_file_is_absent_from_the_tree(repo):
    (repo / "a.txt").unlink()
    sha = synthesize_commit(ShellRunner(), repo, base_sha=_head(repo))
    assert "a.txt" not in _tree_files(repo, sha)


def test_local_state_is_identical_before_and_after(repo):
    """ac2, the destructive-surprise guard: `git add -A` against the DEFAULT
    index would stage the operator's work in progress."""
    _dirty(repo)

    def snapshot():
        return {
            "head": _git("rev-parse", "HEAD", cwd=repo),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo),
            "status": _git("status", "--porcelain", cwd=repo),
            "index": _git("diff", "--cached", "--name-only", cwd=repo),
            "branches": _git("branch", "--list", cwd=repo),
        }

    before = snapshot()
    sha = synthesize_commit(ShellRunner(), repo, base_sha=before["head"])
    after = snapshot()

    assert after == before
    assert before["index"] == ""                     # nothing was staged
    assert sha != before["head"]


def test_the_commit_is_on_no_branch(repo):
    """ac2/ac14: it is not history anyone can build on."""
    _dirty(repo)
    base = _head(repo)
    sha = synthesize_commit(ShellRunner(), repo, base_sha=base)
    assert _git("branch", "--contains", sha, "--all", cwd=repo) == ""
    assert _git("rev-parse", f"{sha}^", cwd=repo) == base


def test_the_parent_is_the_sha_it_was_given_not_a_re_resolved_head(repo):
    """Fix 9 carried onto this path. `inspect` certifies a sha; something else
    committing in this worktree in the meantime (a subagent, a background job —
    this workspace's normal pattern) must not silently re-root the snapshot."""
    _dirty(repo)
    certified = _head(repo)

    (repo / "raced.txt").write_text("raced\n")
    _git("add", "raced.txt", cwd=repo)
    _git("commit", "-qm", "raced after inspection", cwd=repo)
    assert _head(repo) != certified

    sha = synthesize_commit(ShellRunner(), repo, base_sha=certified)
    assert _git("rev-parse", f"{sha}^", cwd=repo) == certified


def test_it_works_in_a_repo_with_no_configured_identity(tmp_path, monkeypatch):
    """`git commit-tree` needs an author; taking it from git config would make
    synthesis fail on a machine that has none. It is pinned to mship instead."""
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)

    path = tmp_path / "no-identity"
    path.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=path)
    (path / "a.txt").write_text("one\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    (path / "a.txt").write_text("edited\n")

    # The identity can only have come from synthesize_commit: nothing in this
    # repo's config supplies one.
    assert subprocess.run(
        ["git", "config", "user.email"], cwd=path, capture_output=True,
        text=True, env={k: v for k, v in _git_env().items()
                        if not k.startswith("GIT_AUTHOR")
                        and not k.startswith("GIT_COMMITTER")},
    ).returncode != 0

    sha = synthesize_commit(ShellRunner(), path, base_sha=_head(path))
    assert "mship" in _git("show", "-s", "--format=%an <%ae>", sha, cwd=path)


def test_commit_signing_cannot_block_synthesis(repo, monkeypatch):
    """`git commit-tree` does not honour `commit.gpgsign` (verified with
    `-c commit.gpgsign=true -c gpg.program=/bin/false`), so an operator with
    signing configured cannot be left waiting on a passphrase prompt that a
    captured-output subprocess never shows anyone."""
    # tests/conftest.py's session fixture force-disables commit.gpgsign for the
    # whole suite via GIT_CONFIG_* env vars, which outrank repo config. Left in
    # place, this test would assert nothing at all.
    for var in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"):
        monkeypatch.delenv(var, raising=False)

    _git("config", "commit.gpgsign", "true", cwd=repo)
    _git("config", "gpg.program", "/bin/false", cwd=repo)
    _dirty(repo)

    # Control: the signing config really is live on this repo — `git commit`
    # under it cannot produce a commit object.
    control = subprocess.run(
        ["git", "commit", "-am", "would sign"], cwd=repo, capture_output=True,
        text=True, env=_git_env(),
    )
    assert control.returncode != 0 and "sign" in control.stderr

    assert synthesize_commit(ShellRunner(), repo, base_sha=_head(repo))


def test_synthesis_from_a_subdirectory_still_captures_the_whole_repo(tmp_path):
    """A `git_root` child's path is a subdirectory of its parent's worktree, so
    synthesis may be invoked there. Verified: `git add -A` with no pathspec is
    whole-tree since git 2.0, and `git write-tree` writes the full index."""
    mono = tmp_path / "mono"
    (mono / "pkg").mkdir(parents=True)
    _git("init", "-q", "-b", "main", ".", cwd=mono)
    (mono / "root.txt").write_text("root\n")
    (mono / "pkg" / "c.txt").write_text("child\n")
    _git("add", "-A", cwd=mono)
    _git("commit", "-qm", "init", cwd=mono)
    (mono / "root.txt").write_text("ROOT-EDIT\n")
    (mono / "pkg" / "c.txt").write_text("CHILD-EDIT\n")

    sha = synthesize_commit(ShellRunner(), mono / "pkg", base_sha=_head(mono))

    # Listed from the repo ROOT: `git ls-tree` filters and strips by the cwd
    # prefix, so running it inside `pkg/` would show a single `c.txt` and hide
    # the very regression this test exists for.
    assert sorted(_tree_files(mono, sha)) == ["pkg/c.txt", "root.txt"]
    assert _git("show", f"{sha}:root.txt", cwd=mono) == "ROOT-EDIT"


def test_a_clean_tree_still_synthesizes_the_same_content(repo):
    """Not a path the CLI takes (clean repos go to origin), but the function
    must not depend on there being changes."""
    base = _head(repo)
    sha = synthesize_commit(ShellRunner(), repo, base_sha=base)
    assert _git("rev-parse", f"{sha}^{{tree}}", cwd=repo) == _git(
        "rev-parse", f"{base}^{{tree}}", cwd=repo
    )


def test_a_git_failure_raises_a_named_error(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(RunTransferError) as exc:
        synthesize_commit(ShellRunner(), not_a_repo, base_sha="HEAD")
    assert "read-tree" in str(exc.value)
