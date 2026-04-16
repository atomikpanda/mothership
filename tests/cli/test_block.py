from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


@pytest.fixture
def configured_app_with_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(tasks={"add-labels": task}))
    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_block(configured_app_with_task: Path):
    result = runner.invoke(app, ["block", "waiting on API key", "--task", "add-labels"])
    assert result.exit_code == 0
    mgr = StateManager(configured_app_with_task / ".mothership")
    state = mgr.load()
    assert state.tasks["add-labels"].blocked_reason == "waiting on API key"
    assert state.tasks["add-labels"].blocked_at is not None


def test_unblock(configured_app_with_task: Path):
    runner.invoke(app, ["block", "waiting", "--task", "add-labels"])
    result = runner.invoke(app, ["unblock", "--task", "add-labels"])
    assert result.exit_code == 0
    mgr = StateManager(configured_app_with_task / ".mothership")
    state = mgr.load()
    assert state.tasks["add-labels"].blocked_reason is None
    assert state.tasks["add-labels"].blocked_at is None


def test_unblock_when_not_blocked(configured_app_with_task: Path):
    result = runner.invoke(app, ["unblock", "--task", "add-labels"])
    assert result.exit_code != 0 or "not blocked" in result.output.lower()


def test_block_no_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    result = runner.invoke(app, ["block", "reason"])
    assert result.exit_code != 0 or "no active task" in result.output.lower()
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_status_shows_blocked(configured_app_with_task: Path):
    runner.invoke(app, ["block", "waiting on API key", "--task", "add-labels"])
    result = runner.invoke(app, ["status", "--task", "add-labels"])
    assert "BLOCKED" in result.output
    assert "waiting on API key" in result.output
