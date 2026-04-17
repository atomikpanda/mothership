"""Tests for the pure `build_context` builder (no CLI, no real git)."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from mship.core.config import WorkspaceConfig, RepoConfig
from mship.core.context import SCHEMA_VERSION, build_context
from mship.core.log import LogManager
from mship.core.reconcile.cache import CachePayload, ReconcileCache
from mship.core.state import Task, TestResult, WorkspaceState
from mship.core.workspace_meta import write_last_sync_at


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


def _build(state, config, log_manager, cwd, state_dir=None, **kw):
    kw.setdefault("git_count", lambda *_: None)
    kw.setdefault("binary_check", _no_binary_check)
    kw.setdefault("dirty_check", lambda _p: None)
    return build_context(
        state, config, log_manager, cwd,
        state_dir=state_dir if state_dir is not None else cwd,
        **kw,
    )


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


# --- Tier 2: drift, main_checkout_clean, fetch/drift timestamps -----------


def test_drift_unknown_when_no_cache(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"u": _task("u")})
    out = _build(state, _config(tmp_path), log_mgr, tmp_path)
    assert out["active_tasks"][0]["drift"] == "unknown"
    assert out["last_drift_check_at"] is None


def test_drift_read_from_cache(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"a": _task("a"), "b": _task("b")})

    cache = ReconcileCache(tmp_path)
    fetched = time.time()
    cache.write(CachePayload(
        fetched_at=fetched, ttl_seconds=300,
        results={
            "a": {"state": "merged", "pr_url": None, "pr_number": None,
                  "base": None, "merge_commit": None, "updated_at": None},
            # 'b' deliberately absent -> "unknown"
        },
        ignored=[],
    ))

    out = _build(state, _config(tmp_path), log_mgr, tmp_path,
                 state_dir=tmp_path, cache=cache)
    by_slug = {t["slug"]: t for t in out["active_tasks"]}
    assert by_slug["a"]["drift"] == "merged"
    assert by_slug["b"]["drift"] == "unknown"
    assert out["last_drift_check_at"] is not None


def test_drift_unknown_when_cache_entry_malformed(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"x": _task("x")})

    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time(), ttl_seconds=300,
        results={"x": {"not_a_state_field": "junk"}},
        ignored=[],
    ))

    out = _build(state, _config(tmp_path), log_mgr, tmp_path,
                 state_dir=tmp_path, cache=cache)
    assert out["active_tasks"][0]["drift"] == "unknown"


def test_main_checkout_clean_dispatches_per_repo(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    cfg = _config(tmp_path)
    seen: list[Path] = []

    def dirty(p: Path) -> Optional[bool]:
        seen.append(p)
        return False  # clean

    out = _build(
        WorkspaceState(), cfg, log_mgr, tmp_path,
        dirty_check=dirty,
    )
    assert out["main_checkout_clean"] == {"repo": True}
    assert seen == [cfg.repos["repo"].path]


def test_main_checkout_clean_reports_dirty(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(
        WorkspaceState(), _config(tmp_path), log_mgr, tmp_path,
        dirty_check=lambda _p: True,
    )
    assert out["main_checkout_clean"] == {"repo": False}


def test_main_checkout_clean_unknown_on_error(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(
        WorkspaceState(), _config(tmp_path), log_mgr, tmp_path,
        dirty_check=lambda _p: None,
    )
    assert out["main_checkout_clean"] == {"repo": None}


def test_main_checkout_clean_skips_git_root_children(tmp_path: Path):
    """Repos with `git_root` set share their parent's checkout — don't double-report."""
    log_mgr = LogManager(tmp_path / "logs")

    parent_dir = tmp_path / "mono"
    parent_dir.mkdir()
    (parent_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    child_dir = parent_dir / "pkg"
    child_dir.mkdir()
    (child_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    cfg = WorkspaceConfig(
        workspace="t",
        repos={
            "mono": RepoConfig(path=parent_dir, type="service"),
            "pkg": RepoConfig(path=child_dir, type="library", git_root="mono"),
        },
    )

    out = _build(
        WorkspaceState(), cfg, log_mgr, tmp_path,
        dirty_check=lambda _p: False,
    )
    assert out["main_checkout_clean"] == {"mono": True}
    assert "pkg" not in out["main_checkout_clean"]


def test_last_workspace_fetch_at_null_when_unset(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path)
    assert out["last_workspace_fetch_at"] is None


def test_last_workspace_fetch_at_round_trips(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    when = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
    write_last_sync_at(tmp_path, when)
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path,
                 state_dir=tmp_path)
    assert out["last_workspace_fetch_at"] == when.isoformat()
