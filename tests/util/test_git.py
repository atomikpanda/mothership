import os
import subprocess
from pathlib import Path

import pytest

from mship.util.git import GitRunner


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


def test_worktree_remove(git_repo: Path):
    runner = GitRunner()
    wt_path = git_repo / ".worktrees" / "feat" / "test-branch"
    runner.worktree_add(repo_path=git_repo, worktree_path=wt_path, branch="feat/test-branch")
    runner.worktree_remove(repo_path=git_repo, worktree_path=wt_path)
    assert not wt_path.exists()


def test_branch_delete(git_repo: Path):
    runner = GitRunner()
    wt_path = git_repo / ".worktrees" / "feat" / "test-branch"
    runner.worktree_add(repo_path=git_repo, worktree_path=wt_path, branch="feat/test-branch")
    runner.worktree_remove(repo_path=git_repo, worktree_path=wt_path)
    runner.branch_delete(repo_path=git_repo, branch="feat/test-branch")
    result = subprocess.run(
        ["git", "branch", "--list", "feat/test-branch"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""


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
