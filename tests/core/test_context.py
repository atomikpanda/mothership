"""Tests for the pure `build_context` builder (no CLI, no real git)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from mship.core.config import WorkspaceConfig, RepoConfig
from mship.core.context import SCHEMA_VERSION, build_context
from mship.core.log import LogManager
from mship.core.state import Task, TestResult, WorkspaceState


def _config(tmp_path: Path) -> WorkspaceConfig:
    """Minimal WorkspaceConfig that satisfies the model validators."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    return WorkspaceConfig(
        workspace="t",
        repos={"repo": RepoConfig(path=repo_dir, type="library")},
    )


def _task(slug: str, **overrides) -> Task:
    base = dict(
        slug=slug,
        description=slug,
        phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["repo"],
        worktrees={},
        branch=f"feat/{slug}",
        base_branch="main",
    )
    base.update(overrides)
    return Task(**base)


def _fake_git_count(answers: dict[tuple[str, str], Optional[int]]):
    """Build a GitCounter that looks up answers by (worktree-name, ref-spec)."""
    def _count(wt: Path, ref: str) -> Optional[int]:
        return answers.get((wt.name, ref))
    return _count


def _no_binary_check() -> Optional[bool]:
    return None


def _build(state, config, log_manager, cwd, **kw):
    kw.setdefault("git_count", lambda *_: None)
    kw.setdefault("binary_check", _no_binary_check)
    return build_context(state, config, log_manager, cwd, **kw)


def test_empty_state_returns_no_active_tasks(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path)
    assert out["schema_version"] == SCHEMA_VERSION
    assert out["active_tasks"] == []
    assert out["cwd_matches_task"] is None
    assert out["cwd_matches_repo"] is None


def test_finished_tasks_are_filtered_out(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={
        "live": _task("live"),
        "done": _task("done", finished_at=datetime.now(timezone.utc)),
    })
    out = _build(state, _config(tmp_path), log_mgr, tmp_path)
    slugs = [t["slug"] for t in out["active_tasks"]]
    assert slugs == ["live"]


def test_task_payload_shape(tmp_path: Path):
    wt = tmp_path / "wt-foo"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    log_mgr.append("foo", "did a thing")
    state = WorkspaceState(tasks={"foo": _task(
        "foo",
        worktrees={"repo": wt},
        active_repo="repo",
        pr_urls={"repo": "https://example/pr/1"},
        test_iteration=3,
        test_results={"repo": TestResult(status="pass", at=datetime.now(timezone.utc))},
    )})

    git = _fake_git_count({
        (wt.name, "@{u}..HEAD"): 2,
        (wt.name, "main..HEAD"): 4,
    })
    out = _build(state, _config(tmp_path), log_mgr, tmp_path, git_count=git)
    task = out["active_tasks"][0]
    assert task["slug"] == "foo"
    assert task["branch"] == "feat/foo"
    assert task["base_branch"] == "main"
    assert task["worktrees"] == {"repo": str(wt)}
    assert task["active_repo"] == "repo"
    assert task["ahead_of_origin"] == {"repo": 2}
    assert task["ahead_of_base"] == {"repo": 4}
    assert task["pr_urls"] == {"repo": "https://example/pr/1"}
    assert task["last_test_state"] == "pass"
    assert task["last_test_iteration"] == 3
    assert task["last_log_entry_at"] is not None


def test_ahead_of_base_is_null_when_base_branch_unset(tmp_path: Path):
    wt = tmp_path / "wt-x"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"x": _task(
        "x", worktrees={"repo": wt}, base_branch=None,
    )})
    git = _fake_git_count({(wt.name, "@{u}..HEAD"): 1})
    out = _build(state, _config(tmp_path), log_mgr, tmp_path, git_count=git)
    task = out["active_tasks"][0]
    assert task["ahead_of_base"] == {"repo": None}
    assert task["ahead_of_origin"] == {"repo": 1}


def test_cwd_inside_worktree_populates_match_fields(tmp_path: Path):
    wt = tmp_path / "wt-match"
    (wt / "src").mkdir(parents=True)
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"m": _task("m", worktrees={"repo": wt})})

    out = _build(state, _config(tmp_path), log_mgr, wt / "src")
    assert out["cwd_matches_task"] == "m"
    assert out["cwd_matches_repo"] == "repo"


def test_cwd_outside_any_worktree_yields_none(tmp_path: Path):
    wt = tmp_path / "wt-other"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"o": _task("o", worktrees={"repo": wt})})

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = _build(state, _config(tmp_path), log_mgr, elsewhere)
    assert out["cwd_matches_task"] is None
    assert out["cwd_matches_repo"] is None


def test_finished_task_does_not_capture_cwd(tmp_path: Path):
    """A finished task's worktree shouldn't claim cwd — it's stale."""
    wt = tmp_path / "wt-done"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"d": _task(
        "d", worktrees={"repo": wt}, finished_at=datetime.now(timezone.utc),
    )})
    out = _build(state, _config(tmp_path), log_mgr, wt)
    assert out["cwd_matches_task"] is None
    assert out["cwd_matches_repo"] is None


def test_binary_check_passthrough(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(
        WorkspaceState(), _config(tmp_path), log_mgr, tmp_path,
        binary_check=lambda: False,
    )
    assert out["mship_binary_matches_editable_install"] is False


def test_no_test_results_yields_null_state(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"q": _task("q")})
    out = _build(state, _config(tmp_path), log_mgr, tmp_path)
    task = out["active_tasks"][0]
    assert task["last_test_state"] is None
    assert task["last_test_iteration"] == 0


def test_most_recent_test_result_wins(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 4, 1, tzinfo=timezone.utc)
    state = WorkspaceState(tasks={"r": _task(
        "r",
        test_results={
            "a": TestResult(status="pass", at=older),
            "b": TestResult(status="fail", at=newer),
        },
    )})
    out = _build(state, _config(tmp_path), log_mgr, tmp_path)
    assert out["active_tasks"][0]["last_test_state"] == "fail"
