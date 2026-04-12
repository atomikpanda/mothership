import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

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


def test_finish_handoff(configured_git_app: Path):
    runner.invoke(app, ["spawn", "handoff test", "--repos", "shared,auth-service"])
    result = runner.invoke(app, ["finish", "--handoff"])
    assert result.exit_code == 0
    handoff_file = configured_git_app / ".mothership" / "handoffs" / "handoff-test.yaml"
    assert handoff_file.exists()


def test_finish_creates_prs(configured_git_app: Path):
    from mship.cli import container as cli_container

    # Spawn a task first
    runner.invoke(app, ["spawn", "test prs", "--repos", "shared"])

    # Mock shell for finish operations
    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        if "gh pr view" in cmd:
            return ShellResult(returncode=0, stdout="body text", stderr="")
        if "gh pr edit" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    # Verify PR URL stored in state
    from mship.core.state import StateManager
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert "test-prs" in state.tasks
    assert state.tasks["test-prs"].pr_urls.get("shared") == "https://github.com/org/shared/pull/1"

    cli_container.shell.reset_override()


def test_finish_gh_not_available(configured_git_app: Path):
    from mship.cli import container as cli_container

    runner.invoke(app, ["spawn", "test no gh", "--repos", "shared"])

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=127, stdout="", stderr="command not found")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish"])
    assert result.exit_code != 0 or "gh" in result.output.lower()

    cli_container.shell.reset_override()


def test_spawn_skip_setup_flag(configured_git_app: Path):
    """--skip-setup should skip the setup task."""
    from mship.cli import container as cli_container
    from unittest.mock import MagicMock
    from mship.util.shell import ShellResult, ShellRunner

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["spawn", "skip flag test", "--repos", "shared", "--skip-setup"])
    assert result.exit_code == 0, result.output
    # run_task should not have been called for setup
    mock_shell.run_task.assert_not_called()

    cli_container.shell.reset_override()


def test_spawn_shows_setup_warnings(configured_git_app: Path):
    """Setup failures should appear as warnings in output."""
    from mship.cli import container as cli_container
    from unittest.mock import MagicMock
    from mship.util.shell import ShellResult, ShellRunner

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(
        returncode=1, stdout="", stderr="pnpm install failed"
    )
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["spawn", "warning flag test", "--repos", "shared"])
    assert result.exit_code == 0, result.output
    # Setup failure should appear in output as a warning
    assert "setup failed" in result.output.lower() or "pnpm install failed" in result.output

    cli_container.shell.reset_override()
