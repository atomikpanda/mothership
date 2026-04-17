"""Tests for the `mship context` CLI command (JSON wire format)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState


def _bootstrap(tmp_path: Path, slugs: list[str]) -> tuple[Path, Path]:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    tasks = {
        s: Task(
            slug=s, description=s, phase="dev",
            created_at=datetime.now(timezone.utc),
            affected_repos=["mothership"],
            branch=f"feat/{s}",
            base_branch="main",
        )
        for s in slugs
    }
    StateManager(state_dir).save(WorkspaceState(tasks=tasks))
    return cfg, state_dir


def _reset_container():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.log_manager.reset()


def test_context_emits_valid_json(tmp_path: Path):
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, ["alpha", "beta"])

    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["schema_version"] == "1"
        slugs = sorted(t["slug"] for t in data["active_tasks"])
        assert slugs == ["alpha", "beta"]
        for task in data["active_tasks"]:
            assert task["base_branch"] == "main"
            assert task["phase"] == "dev"
            assert task["finished_at"] is None
            assert task["drift"] == "unknown"  # no reconcile cache present
        assert data["main_checkout_clean"] == {}  # config has no repos
        assert data["last_workspace_fetch_at"] is None
        assert data["last_drift_check_at"] is None
    finally:
        _reset_container()


def test_context_empty_workspace(tmp_path: Path):
    runner = CliRunner()
    cfg, state_dir = _bootstrap(tmp_path, [])

    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["context"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["active_tasks"] == []
        assert data["cwd_matches_task"] is None
        assert data["cwd_matches_repo"] is None
    finally:
        _reset_container()
