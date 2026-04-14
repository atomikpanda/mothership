import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager


runner = CliRunner()


@pytest.fixture
def test_workspace(workspace_with_git):
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    container.state_manager.reset()
    container.log_manager.reset()
    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def _spawn(description="first"):
    result = runner.invoke(
        app, ["spawn", description, "--repos", "shared", "--force-audit"],
    )
    assert result.exit_code == 0, result.output


def test_first_test_run_writes_iteration_file_and_log_entry(test_workspace):
    _spawn()
    result = runner.invoke(app, ["test"])
    # Test may pass or fail depending on Taskfile stub; either exit code accepted.
    # The important thing: iteration file and log entry exist.
    task_slug = "first"
    iter_path = test_workspace / ".mothership" / "test-runs" / task_slug / "1.json"
    assert iter_path.exists(), result.output
    data = json.loads(iter_path.read_text())
    assert data["iteration"] == 1
    assert "shared" in data["repos"]

    state = StateManager(test_workspace / ".mothership").load()
    assert state.tasks[task_slug].test_iteration == 1

    # Auto-appended log entry
    from mship.core.log import LogManager
    log_mgr = LogManager(test_workspace / ".mothership" / "logs")
    entries = log_mgr.read(task_slug)
    assert any(e.action == "ran tests" and e.iteration == 1 for e in entries)


def test_second_test_run_shows_still_passing_or_still_failing(test_workspace):
    _spawn("second")
    r1 = runner.invoke(app, ["test"])
    r2 = runner.invoke(app, ["test"])
    # One of the labels must appear in r2 output (TTY or not; CliRunner is non-TTY so JSON).
    try:
        payload = json.loads(r2.output)
        tags = payload["diff"]["tags"]
        assert "shared" in tags
        assert tags["shared"] in {"still passing", "still failing"}
    except json.JSONDecodeError:
        assert ("still passing" in r2.output) or ("still failing" in r2.output)


def test_no_diff_flag_suppresses_diff(test_workspace):
    _spawn("nodiff")
    runner.invoke(app, ["test"])
    result = runner.invoke(app, ["test", "--no-diff"])
    # No tags section, no "still passing" / "new failure" / etc. in either plain or JSON mode.
    try:
        payload = json.loads(result.output)
        assert "diff" not in payload
    except json.JSONDecodeError:
        for label in ("still passing", "still failing", "new failure", "fix", "regression"):
            assert label not in result.output
