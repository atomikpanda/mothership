from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.log import LogManager

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
    mgr.save(WorkspaceState(current_task="add-labels", tasks={"add-labels": task}))

    # Create the log file
    log_mgr = LogManager(state_dir / "logs")
    log_mgr.create("add-labels")

    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_log_append(configured_app_with_task: Path):
    result = runner.invoke(app, ["log", "Refactored auth controller"])
    assert result.exit_code == 0


def test_log_read(configured_app_with_task: Path):
    runner.invoke(app, ["log", "First entry"])
    runner.invoke(app, ["log", "Second entry"])
    result = runner.invoke(app, ["log"])
    assert result.exit_code == 0
    assert "First entry" in result.output
    assert "Second entry" in result.output


def test_log_last_n(configured_app_with_task: Path):
    runner.invoke(app, ["log", "First"])
    runner.invoke(app, ["log", "Second"])
    runner.invoke(app, ["log", "Third"])
    result = runner.invoke(app, ["log", "--last", "1"])
    assert result.exit_code == 0
    assert "Third" in result.output
    assert "First" not in result.output


def test_log_no_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    result = runner.invoke(app, ["log"])
    assert result.exit_code != 0 or "No active task" in result.output
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
