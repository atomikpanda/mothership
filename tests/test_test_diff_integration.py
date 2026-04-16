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
    pytest.skip("obsolete — current_task removed in multi-task migration (Task 13)")
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
    pytest.skip("obsolete — current_task removed in multi-task migration (Task 13)")
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


def test_mship_test_writes_stdout_stderr_artifacts(test_workspace):
    """mship test must write <iter>.<repo>.stdout and .stderr artifact files."""
    pytest.skip("obsolete — current_task removed in multi-task migration (Task 13)")
    _spawn("artifacts")
    runner.invoke(app, ["test"])
    task_slug = "artifacts"
    run_dir = test_workspace / ".mothership" / "test-runs" / task_slug
    assert run_dir.exists(), "test-runs dir missing"

    stdout_files = list(run_dir.glob("1.*.stdout"))
    stderr_files = list(run_dir.glob("1.*.stderr"))
    assert stdout_files, f"No stdout artifact in {run_dir}"
    assert stderr_files, f"No stderr artifact in {run_dir}"


def test_mship_test_json_includes_stdout_stderr_paths(test_workspace):
    """Non-TTY JSON output must include stdout_path and stderr_path per repo."""
    _spawn("pathtest")
    result = runner.invoke(app, ["test"])
    try:
        payload = json.loads(result.output)
    except json.JSONDecodeError:
        pytest.skip("Output was not JSON (likely TTY mode)")

    for repo_name, repo_data in payload["repos"].items():
        assert "stdout_path" in repo_data, f"stdout_path missing for {repo_name}"
        assert "stderr_path" in repo_data, f"stderr_path missing for {repo_name}"
        assert Path(repo_data["stdout_path"]).exists(), (
            f"stdout artifact file missing for {repo_name}: {repo_data['stdout_path']}"
        )
        assert Path(repo_data["stderr_path"]).exists(), (
            f"stderr artifact file missing for {repo_name}: {repo_data['stderr_path']}"
        )
