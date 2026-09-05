"""Tests for `mship reconcile` CLI."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.reconcile.cache import CachePayload, ReconcileCache, DEFAULT_TTL_SECONDS
from mship.core.reconcile.detect import UpstreamState
from mship.core.reconcile.gate import Decision
from mship.core.state import StateManager, Task, WorkspaceState


def _task(slug: str) -> Task:
    return Task(
        slug=slug,
        description=slug,
        phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"],
        worktrees={},
        branch=f"feat/{slug}",
    )


def _bootstrap(tmp_path: Path, slugs: list[str]) -> tuple[Path, Path]:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    tasks = {s: _task(s) for s in slugs}
    StateManager(state_dir).save(WorkspaceState(tasks=tasks))
    return cfg, state_dir


def _reset_container():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_reconcile_prints_table_from_cache(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, ["alpha"])

    decision = Decision(
        slug="alpha", state=UpstreamState.merged,
        pr_url="https://example/pr/1", pr_number=1,
        base="main", merge_commit="abc123", updated_at=None,
    )
    monkeypatch.setattr(
        "mship.cli.reconcile.reconcile_now",
        lambda state, **kw: {"alpha": decision},
    )

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["reconcile"])
        assert result.exit_code == 0, result.output
        assert "merged" in result.output
        assert "alpha" in result.output
    finally:
        _reset_container()


def test_reconcile_json_output(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, ["beta"])

    decision = Decision(
        slug="beta", state=UpstreamState.in_sync,
        pr_url=None, pr_number=None, base="main",
        merge_commit=None, updated_at=None,
    )
    monkeypatch.setattr(
        "mship.cli.reconcile.reconcile_now",
        lambda state, **kw: {"beta": decision},
    )

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["reconcile", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["tasks"][0]["slug"] == "beta"
    finally:
        _reset_container()


def test_reconcile_add_ignore(tmp_path: Path):
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, ["gamma"])

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["reconcile", "--ignore", "gamma"])
        assert result.exit_code == 0, result.output
        cache = ReconcileCache(state_dir)
        assert "gamma" in cache.read_ignores()
    finally:
        _reset_container()


def _seed_merged_cache(
    state_dir: Path, slug: str, live_slugs: list[str],
) -> None:
    cache = ReconcileCache(state_dir)
    cache.write(CachePayload(
        fetched_at=time.time(), ttl_seconds=DEFAULT_TTL_SECONDS,
        results={
            slug: {
                "state": "merged",
                "pr_url": "https://example/pr/1",
                "pr_number": 1,
                "base": "main",
                "merge_commit": "abc123",
                "updated_at": "2026-05-10T00:00:00Z",
            }
        },
        ignored=[],
        base_context={live_slug: None for live_slug in live_slugs},
    ))


def _bootstrap_with_current(tmp_path: Path, slug: str) -> tuple[Path, Path]:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    task = _task(slug)
    StateManager(state_dir).save(WorkspaceState(tasks={slug: task}))
    return cfg, state_dir


def test_finish_blocks_on_merged_drift(tmp_path: Path):
    runner = CliRunner()
    cfg, state_dir = _bootstrap_with_current(tmp_path, "alpha")
    _seed_merged_cache(state_dir, "alpha", ["alpha"])

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["finish"])
        assert result.exit_code != 0, result.output
        assert "merged" in result.output
        assert "bypass-reconcile" in result.output
    finally:
        _reset_container()


def test_finish_bypass_lets_through(tmp_path: Path):
    runner = CliRunner()
    cfg, state_dir = _bootstrap_with_current(tmp_path, "alpha")
    _seed_merged_cache(state_dir, "alpha", ["alpha"])

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["finish", "--bypass-reconcile"])
        # The gate-block message must NOT appear. finish may still fail for
        # other reasons (no commits, no gh, etc.) — we only check the gate.
        assert "upstream drift" not in result.output
    finally:
        _reset_container()


def test_finish_gate_scoped_to_finishing_task_not_blocked_by_other_task_drift(tmp_path: Path):
    """#455 Part 2: task 'beta' has merged-PR drift (would block finish on its
    own), but we're finishing unrelated task 'alpha'. alpha's finish must not
    be refused because of beta's drift — the gate scopes to the task being
    finished, not every task in the workspace.
    """
    runner = CliRunner()
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    tasks = {"alpha": _task("alpha"), "beta": _task("beta")}
    StateManager(state_dir).save(WorkspaceState(tasks=tasks))
    _seed_merged_cache(state_dir, "beta", ["alpha", "beta"])

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["finish", "--task", "alpha"])
        assert "upstream drift" not in result.output
        assert "beta" not in result.output
    finally:
        _reset_container()


@pytest.mark.parametrize(
    "base_args",
    [
        ["--base", "release"],
        ["--base", "fallback", "--base-map", "mothership=release"],
    ],
)
def test_finish_reconcile_uses_explicit_base_inputs(
    tmp_path: Path, monkeypatch, base_args: list[str],
):
    """The finish drift gate must resolve the same explicit base as PR creation."""
    from mship.core.reconcile.detect import GitSnapshot, PRSnapshot

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    repo_dir = tmp_path / "mothership"
    repo_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  mothership:\n"
        "    path: ./mothership\n"
        "    type: library\n"
        "    base_branch: main\n"
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"alpha": _task("alpha")}))
    fetch_calls: list[list[str]] = []

    def _fetch_prs(branches):
        fetch_calls.append(list(branches))
        return {
            "feat/alpha": PRSnapshot(
                head_ref="feat/alpha", state="OPEN", base_ref="release",
                merge_commit=None, url="https://example/pr/1", updated_at="z",
            ),
        }
    monkeypatch.setattr(
        "mship.core.reconcile.fetch.fetch_pr_snapshots",
        _fetch_prs,
    )
    monkeypatch.setattr(
        "mship.core.reconcile.fetch.collect_git_snapshots",
        lambda worktrees: {
            "feat/alpha": GitSnapshot(has_upstream=True, behind=0, ahead=1),
        },
    )

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = CliRunner().invoke(app, ["finish", *base_args])
        assert "upstream drift" not in result.output, result.output
        assert "base_changed" not in result.output, result.output
        assert fetch_calls == [["feat/alpha"]]
        assert "reconcile unavailable" not in result.output, result.output
    finally:
        _reset_container()


def test_reconcile_clear_ignores(tmp_path: Path):
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, ["a", "b"])

    cache = ReconcileCache(state_dir)
    cache.write(CachePayload(
        fetched_at=time.time(), ttl_seconds=DEFAULT_TTL_SECONDS,
        results={}, ignored=["a", "b"],
    ))

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["reconcile", "--clear-ignores"])
        assert result.exit_code == 0, result.output
        assert cache.read_ignores() == []
    finally:
        _reset_container()


@pytest.mark.parametrize(
    ("args", "initial_ignores", "expected_ignores"),
    [
        (["--ignore", "gamma"], [], ["gamma"]),
        (["--clear-ignores"], ["gamma"], []),
    ],
)
def test_reconcile_cache_mutations_do_not_require_valid_repo_paths(
    tmp_path: Path,
    args: list[str],
    initial_ignores: list[str],
    expected_ignores: list[str],
):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  mothership:\n"
        "    path: ./missing\n"
        "    type: library\n"
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"gamma": _task("gamma")}))
    cache = ReconcileCache(state_dir)
    cache.write(CachePayload(
        fetched_at=time.time(),
        ttl_seconds=DEFAULT_TTL_SECONDS,
        results={},
        ignored=initial_ignores,
    ))

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = CliRunner().invoke(app, ["reconcile", *args])
        assert result.exit_code == 0, result.output
        assert cache.read_ignores() == expected_ignores
    finally:
        _reset_container()


def test_reconcile_normal_does_not_require_valid_repo_paths(tmp_path: Path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  mothership:\n"
        "    path: ./missing\n"
        "    type: library\n"
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"gamma": _task("gamma")}))
    cache = ReconcileCache(state_dir)
    cache.write(CachePayload(
        fetched_at=time.time(),
        ttl_seconds=DEFAULT_TTL_SECONDS,
        results={"gamma": {"state": "in_sync"}},
        ignored=[],
        base_context={"gamma": None},
    ))

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = CliRunner().invoke(app, ["reconcile", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["tasks"][0]["slug"] == "gamma"
        assert data["tasks"][0]["state"] == "in_sync"
        with pytest.raises(ValueError, match="path does not exist"):
            container.config()
    finally:
        _reset_container()
