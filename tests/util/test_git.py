import os
import subprocess
from pathlib import Path

import pytest

from mship.util.git import GitRunner, WorktreeRemovalRefused


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo for testing."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )
    return tmp_path


def test_worktree_add(git_repo: Path):
    runner = GitRunner()
    wt_path = git_repo / ".worktrees" / "feat" / "test-branch"
    runner.worktree_add(repo_path=git_repo, worktree_path=wt_path, branch="feat/test-branch")
    assert wt_path.exists()
    assert (wt_path / ".git").exists()


def test_worktree_remove_pins_head(git_repo: Path):
    runner = GitRunner()
    wt_path = git_repo / ".worktrees" / "feat" / "test-branch"
    runner.worktree_add(repo_path=git_repo, worktree_path=wt_path, branch="feat/test-branch")

    recovery_ref = runner.worktree_remove(repo_path=git_repo, worktree_path=wt_path)

    assert not wt_path.exists()
    pinned = subprocess.run(
        ["git", "rev-parse", "--verify", recovery_ref],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert pinned
    branch = subprocess.run(
        ["git", "branch", "--list", "feat/test-branch"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "feat/test-branch"


def test_worktree_remove_refuses_dirty_content(git_repo: Path):
    runner = GitRunner()
    (git_repo / "tracked.txt").write_text("before")
    _run(["git", "add", "tracked.txt"], git_repo)
    _run(["git", "commit", "-m", "tracked"], git_repo)
    wt_path = git_repo / ".worktrees" / "feat" / "dirty"
    runner.worktree_add(repo_path=git_repo, worktree_path=wt_path, branch="feat/dirty")
    (wt_path / "tracked.txt").write_text("after")
    (wt_path / "staged.txt").write_text("staged")
    _run(["git", "add", "staged.txt"], wt_path)
    (wt_path / "untracked.txt").write_text("untracked")

    with pytest.raises(WorktreeRemovalRefused, match="tracked, untracked, or ignored"):
        runner.worktree_remove(repo_path=git_repo, worktree_path=wt_path)

    assert (wt_path / "tracked.txt").read_text() == "after"


def test_worktree_remove_refuses_ignored_content(git_repo: Path):
    runner = GitRunner()
    (git_repo / ".gitignore").write_text("*.ignored\n")
    _run(["git", "add", ".gitignore"], git_repo)
    _run(["git", "commit", "-m", "ignore"], git_repo)
    wt_path = git_repo / ".worktrees" / "feat" / "ignored"
    runner.worktree_add(repo_path=git_repo, worktree_path=wt_path, branch="feat/ignored")
    (wt_path / "cache.ignored").write_text("do not remove")

    with pytest.raises(WorktreeRemovalRefused, match="tracked, untracked, or ignored"):
        runner.worktree_remove(repo_path=git_repo, worktree_path=wt_path)

    assert (wt_path / "cache.ignored").read_text() == "do not remove"


def test_worktree_remove_pins_detached_local_commit_through_gc(git_repo: Path, tmp_path: Path):
    runner = GitRunner()
    wt_path = tmp_path / "detached"
    runner.worktree_add_detached(repo_path=git_repo, worktree_path=wt_path, ref="HEAD")
    (wt_path / "detached.txt").write_text("preserve")
    _run(["git", "add", "detached.txt"], wt_path)
    _run(["git", "commit", "-m", "detached"], wt_path)
    detached_head = _rev(wt_path)

    recovery_ref = runner.worktree_remove(repo_path=git_repo, worktree_path=wt_path)
    _run(["git", "gc", "--prune=now"], git_repo)

    assert not wt_path.exists()
    assert _rev(git_repo, recovery_ref) == detached_head
    subprocess.run(
        ["git", "cat-file", "-e", f"{detached_head}^{{commit}}"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )


def test_worktree_remove_error_preserves_worktree(git_repo: Path):
    runner = GitRunner()
    (git_repo / "preserved.txt").write_text("preserved")
    _run(["git", "add", "preserved.txt"], git_repo)
    _run(["git", "commit", "-m", "preserved"], git_repo)
    wt_path = git_repo / ".worktrees" / "feat" / "error"
    runner.worktree_add(repo_path=git_repo, worktree_path=wt_path, branch="feat/error")
    subprocess.run(
        ["git", "worktree", "lock", str(wt_path)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    with pytest.raises(WorktreeRemovalRefused, match="locked"):
        runner.worktree_remove(repo_path=git_repo, worktree_path=wt_path)

    assert (wt_path / "preserved.txt").read_text() == "preserved"


def test_worktree_remove_refuses_arbitrary_directory(git_repo: Path, tmp_path: Path):
    runner = GitRunner()
    arbitrary = tmp_path / "not-a-worktree"
    arbitrary.mkdir()
    (arbitrary / "keep.txt").write_text("keep")

    with pytest.raises(WorktreeRemovalRefused, match="unregistered"):
        runner.worktree_remove(repo_path=git_repo, worktree_path=arbitrary)

    assert (arbitrary / "keep.txt").read_text() == "keep"


def test_worktree_remove_refuses_canonical_worktree(git_repo: Path):
    runner = GitRunner()

    with pytest.raises(WorktreeRemovalRefused, match="canonical"):
        runner.worktree_remove(repo_path=git_repo, worktree_path=git_repo)

    assert (git_repo / ".git").is_dir()


def test_is_ignored_false(git_repo: Path):
    runner = GitRunner()
    assert runner.is_ignored(git_repo, ".worktrees") is False


def test_is_ignored_true(git_repo: Path):
    (git_repo / ".gitignore").write_text(".worktrees\n")
    runner = GitRunner()
    assert runner.is_ignored(git_repo, ".worktrees") is True


def test_add_to_gitignore(git_repo: Path):
    runner = GitRunner()
    runner.add_to_gitignore(git_repo, ".worktrees")
    content = (git_repo / ".gitignore").read_text()
    assert ".worktrees" in content


def test_add_to_gitignore_existing_file(git_repo: Path):
    (git_repo / ".gitignore").write_text("node_modules\n")
    runner = GitRunner()
    runner.add_to_gitignore(git_repo, ".worktrees")
    content = (git_repo / ".gitignore").read_text()
    assert "node_modules" in content
    assert ".worktrees" in content


def test_has_uncommitted_changes_clean(git_repo: Path):
    runner = GitRunner()
    assert runner.has_uncommitted_changes(git_repo) is False


def test_has_uncommitted_changes_dirty(git_repo: Path):
    (git_repo / "file.txt").write_text("hello")
    runner = GitRunner()
    assert runner.has_uncommitted_changes(git_repo) is True


def test_worktree_add_detached(tmp_path):
    import subprocess
    from mship.util.git import GitRunner
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"],
                   cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         check=True, capture_output=True, text=True).stdout.strip()
    git = GitRunner()
    wt = tmp_path / "wt"
    git.worktree_add_detached(repo_path=repo, worktree_path=wt, ref=sha)
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    assert head == sha
    branch = subprocess.run(["git", "-C", str(wt), "symbolic-ref", "-q", "HEAD"],
                            capture_output=True, text=True).returncode
    assert branch != 0, "expected detached HEAD (symbolic-ref returns nonzero)"


def test_fetch_remote_ref_succeeds(tmp_path):
    """Smoke: fetch_remote_ref returns True when origin has the branch."""
    import subprocess
    from mship.util.git import GitRunner
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"],
                   cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone,
                   check=True, capture_output=True)
    git = GitRunner()
    assert git.fetch_remote_ref(repo_path=clone, ref="main") is True


def test_fetch_remote_ref_returns_false_on_failure(tmp_path):
    """Returns False when origin doesn't have the ref (or no remote)."""
    import subprocess
    from mship.util.git import GitRunner
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    git = GitRunner()
    assert git.fetch_remote_ref(repo_path=repo, ref="nonexistent") is False


# ---------------------------------------------------------------------------
# Task 1: GitRunner helpers — start-point, has_remote, fast_forward_if_clean
# ---------------------------------------------------------------------------

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"}


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=ENV)


def _rev(cwd, ref="HEAD"):
    return subprocess.run(["git", "rev-parse", ref], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo_with_origin_ahead(tmp_path: Path):
    """Return (repo, origin_tip) where `repo`'s local main is one commit behind origin/main."""
    repo = tmp_path / "svc"
    repo.mkdir()
    _run(["git", "init", "-b", "main", str(repo)], tmp_path)
    (repo / "a.txt").write_text("1")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "c1"], repo)
    origin = tmp_path / "svc-origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)], tmp_path)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "origin", "main"], repo)
    clone = tmp_path / "svc-clone"
    _run(["git", "clone", str(origin), str(clone)], tmp_path)
    (clone / "b.txt").write_text("2")
    _run(["git", "add", "-A"], clone)
    _run(["git", "commit", "-m", "c2"], clone)
    _run(["git", "push", "origin", "main"], clone)
    # local `repo` still at c1; fetch so origin/main is known locally
    _run(["git", "fetch", "origin", "main"], repo)
    return repo, _rev(clone)


def test_worktree_add_with_start_point_branches_from_ref(tmp_path):
    repo, origin_tip = _repo_with_origin_ahead(tmp_path)
    git = GitRunner()
    wt = tmp_path / "wt"
    git.worktree_add(repo, wt, "feat/x", start_point="origin/main")
    assert _rev(wt) == origin_tip  # cut from origin tip, not local HEAD


def test_worktree_add_without_start_point_uses_local_head(tmp_path):
    repo, origin_tip = _repo_with_origin_ahead(tmp_path)
    git = GitRunner()
    wt = tmp_path / "wt"
    git.worktree_add(repo, wt, "feat/x")
    assert _rev(wt) == _rev(repo)   # local HEAD (behind)
    assert _rev(wt) != origin_tip


def test_has_remote(tmp_path):
    repo, _ = _repo_with_origin_ahead(tmp_path)
    git = GitRunner()
    assert git.has_remote(repo) is True
    bare = tmp_path / "no-remote"
    bare.mkdir()
    _run(["git", "init", "-b", "main", str(bare)], tmp_path)
    assert git.has_remote(bare) is False


def test_fast_forward_if_clean_advances_clean_behind_base(tmp_path):
    repo, origin_tip = _repo_with_origin_ahead(tmp_path)
    git = GitRunner()
    assert git.fast_forward_if_clean(repo, "main") is True
    assert _rev(repo, "main") == origin_tip


def test_fast_forward_if_clean_skips_dirty_tree(tmp_path):
    repo, origin_tip = _repo_with_origin_ahead(tmp_path)
    (repo / "dirty.txt").write_text("uncommitted")
    git = GitRunner()
    assert git.fast_forward_if_clean(repo, "main") is False
    assert _rev(repo, "main") != origin_tip   # untouched


# ---------------------------------------------------------------------------
# ref_exists — used by `spawn --base` to validate the base branch (#42)
# ---------------------------------------------------------------------------

def test_ref_exists_true_for_existing_branch(git_repo: Path):
    git = GitRunner()
    _run(["git", "branch", "feat/base"], git_repo)
    assert git.ref_exists(git_repo, "feat/base") is True


def test_ref_exists_false_for_missing_branch(git_repo: Path):
    git = GitRunner()
    assert git.ref_exists(git_repo, "feat/nope") is False


def test_ref_exists_true_for_remote_tracking_ref(tmp_path):
    repo, _ = _repo_with_origin_ahead(tmp_path)
    git = GitRunner()
    # origin/main was fetched by the fixture
    assert git.ref_exists(repo, "origin/main") is True
    assert git.ref_exists(repo, "origin/does-not-exist") is False


