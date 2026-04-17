"""Unit tests for src/mship/core/dispatch.py."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.dispatch import SkillRef, canonical_skills, resolve_repo
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
