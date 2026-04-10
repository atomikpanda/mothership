import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager

runner = CliRunner()


@pytest.fixture
def configured_git_app(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)
    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_spawn(configured_git_app: Path):
    result = runner.invoke(app, ["spawn", "add labels to tasks", "--repos", "shared"])
    assert result.exit_code == 0, result.output
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert "add-labels-to-tasks" in state.tasks
    assert state.current_task == "add-labels-to-tasks"


def test_spawn_all_repos(configured_git_app: Path):
    result = runner.invoke(app, ["spawn", "big change"])
    assert result.exit_code == 0, result.output
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    task = state.tasks["big-change"]
    assert set(task.affected_repos) == {"shared", "auth-service", "api-gateway"}


def test_worktrees_list(configured_git_app: Path):
    runner.invoke(app, ["spawn", "test list", "--repos", "shared"])
    result = runner.invoke(app, ["worktrees"])
    assert result.exit_code == 0
    assert "test-list" in result.output


def test_abort(configured_git_app: Path):
    runner.invoke(app, ["spawn", "to abort", "--repos", "shared"])
    result = runner.invoke(app, ["abort", "--yes"])
    assert result.exit_code == 0
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert state.current_task is None
    assert "to-abort" not in state.tasks
