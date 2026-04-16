"""Tests for `mship reconcile` CLI."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

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
    StateManager(state_dir).save(WorkspaceState(tasks=tasks, current_task=None))
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


def _seed_merged_cache(state_dir: Path, slug: str) -> None:
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
                "updated_at": None,
            }
        },
        ignored=[],
    ))


def _bootstrap_with_current(tmp_path: Path, slug: str) -> tuple[Path, Path]:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    task = _task(slug)
    StateManager(state_dir).save(WorkspaceState(tasks={slug: task}, current_task=slug))
    return cfg, state_dir


def test_finish_blocks_on_merged_drift(tmp_path: Path):
    runner = CliRunner()
    cfg, state_dir = _bootstrap_with_current(tmp_path, "alpha")
    _seed_merged_cache(state_dir, "alpha")

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
    _seed_merged_cache(state_dir, "alpha")

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
