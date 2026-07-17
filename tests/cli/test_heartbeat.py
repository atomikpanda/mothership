from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.cli.output import reset_output_settings
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
        slug="add-labels", description="Add labels", phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"], branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(tasks={"add-labels": task}))
    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    # The `--json` global flag is process-global (set once by the top-level
    # callback, never auto-reset to False); clear it so the TTY-shape assertions
    # in other suites (e.g. test_output.py) aren't polluted by our --json test.
    reset_output_settings()


def test_heartbeat_stamps_last_activity(configured_app_with_task: Path):
    result = runner.invoke(app, ["heartbeat", "--task", "add-labels"])
    assert result.exit_code == 0, result.output
    state = StateManager(configured_app_with_task / ".mothership").load()
    assert state.tasks["add-labels"].last_activity_at is not None


def test_heartbeat_has_no_other_side_effects(configured_app_with_task: Path):
    before = StateManager(configured_app_with_task / ".mothership").load().tasks["add-labels"]
    runner.invoke(app, ["heartbeat", "--task", "add-labels"])
    after = StateManager(configured_app_with_task / ".mothership").load().tasks["add-labels"]
    # Only last_activity_at may differ.
    b = before.model_dump(exclude={"last_activity_at"})
    a = after.model_dump(exclude={"last_activity_at"})
    assert a == b


def test_heartbeat_json_output(configured_app_with_task: Path):
    result = runner.invoke(app, ["--json", "heartbeat", "--task", "add-labels"])
    assert result.exit_code == 0, result.output
    import json
    payload = json.loads(result.output)
    assert payload["task"] == "add-labels"
    assert payload["last_activity_at"] is not None


def test_heartbeat_unknown_task_errors(configured_app_with_task: Path):
    result = runner.invoke(app, ["heartbeat", "--task", "ghost"])
    assert result.exit_code != 0
