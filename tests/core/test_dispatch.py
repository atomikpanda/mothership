"""Unit tests for src/mship/core/dispatch.py."""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.dispatch import BaseShaInfo, SkillRef, canonical_skills, collect_base_sha_info, resolve_repo
from mship.core.state import Task


def test_canonical_skills_returns_expected_four_in_order():
    src = Path("/fake/pkg/skills")
    refs = canonical_skills(src)
    assert [r.name for r in refs] == [
        "working-with-mothership",
        "test-driven-development",
        "finishing-a-development-branch",
        "verification-before-completion",
    ]
    for r in refs:
        assert isinstance(r, SkillRef)
        assert r.path == src / r.name / "SKILL.md"


def _task(worktrees: dict[str, Path], active_repo: str | None = None) -> Task:
    return Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=list(worktrees.keys()),
        worktrees=worktrees, branch="feat/t",
        active_repo=active_repo,
    )


def test_resolve_repo_flag_wins(tmp_path: Path):
    t = _task({"a": tmp_path / "a", "b": tmp_path / "b"}, active_repo="a")
    assert resolve_repo(t, repo_flag="b") == "b"


def test_resolve_repo_falls_back_to_active_repo(tmp_path: Path):
    t = _task({"a": tmp_path / "a", "b": tmp_path / "b"}, active_repo="b")
    assert resolve_repo(t, repo_flag=None) == "b"


def test_resolve_repo_uses_sole_worktree_when_unambiguous(tmp_path: Path):
    t = _task({"only": tmp_path / "only"})
    assert resolve_repo(t, repo_flag=None) == "only"


def test_resolve_repo_errors_when_multiple_and_unambiguous(tmp_path: Path):
    t = _task({"a": tmp_path / "a", "b": tmp_path / "b"})
    with pytest.raises(ValueError, match="affects 2 repos"):
        resolve_repo(t, repo_flag=None)


def test_resolve_repo_errors_on_unknown_flag(tmp_path: Path):
    t = _task({"a": tmp_path / "a"})
    with pytest.raises(ValueError, match="unknown repo"):
        resolve_repo(t, repo_flag="nope")


def test_resolve_repo_ignores_stale_active_repo(tmp_path: Path):
    """`active_repo` pointing at a missing worktree should fall through, not crash."""
    t = _task({"a": tmp_path / "a"}, active_repo="deleted")
    assert resolve_repo(t, repo_flag=None) == "a"


def test_resolve_repo_errors_on_empty_worktrees():
    t = _task({})
    with pytest.raises(ValueError, match="affects 0 repos"):
        resolve_repo(t, repo_flag=None)


def _git(args: list[str], cwd: Path, env_extra: dict | None = None):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if env_extra:
        env.update(env_extra)
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _dispatch_git_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare origin + working clone with one initial commit on main."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)],
                   check=True, capture_output=True)
    (clone / "README.md").write_text("init\n")
    _git(["add", "."], cwd=clone)
    _git(["commit", "-qm", "init"], cwd=clone)
    _git(["push", "-q", "origin", "main"], cwd=clone)
    return origin, clone


def test_base_sha_info_clean_state(tmp_path: Path):
    _, clone = _dispatch_git_fixture(tmp_path)
    info = collect_base_sha_info(clone, base_branch="main")
    assert isinstance(info, BaseShaInfo)
    assert info.base_sha == info.origin_base_sha == info.head_sha
    assert "in sync" in info.summary
    assert info.has_upstream is True


def test_base_sha_info_ahead(tmp_path: Path):
    _, clone = _dispatch_git_fixture(tmp_path)
    _git(["checkout", "-b", "feat/x"], cwd=clone)
    (clone / "x.txt").write_text("x\n")
    _git(["add", "."], cwd=clone)
    _git(["commit", "-qm", "x"], cwd=clone)
    info = collect_base_sha_info(clone, base_branch="main")
    assert "1 commit ahead" in info.summary
    assert info.head_sha != info.base_sha


def test_base_sha_info_no_upstream(tmp_path: Path):
    _, clone = _dispatch_git_fixture(tmp_path)
    # Drop the remote so origin/main lookup fails
    _git(["remote", "remove", "origin"], cwd=clone)
    info = collect_base_sha_info(clone, base_branch="main")
    assert info.has_upstream is False
    assert "no upstream" in info.summary
    assert info.origin_base_sha is None
