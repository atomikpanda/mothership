import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


@pytest.fixture
def configured_app(workspace: Path):
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    (workspace / ".mothership").mkdir(exist_ok=True)
    yield
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_status_no_task(configured_app):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "No active task" in result.output


def test_status_with_task(configured_app, workspace: Path):
    state_dir = workspace / ".mothership"
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels to tasks",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(current_task="add-labels", tasks={"add-labels": task}))

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "add-labels" in result.output
    assert "dev" in result.output


def test_graph(configured_app):
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "shared" in result.output
    assert "auth-service" in result.output
    assert "api-gateway" in result.output
