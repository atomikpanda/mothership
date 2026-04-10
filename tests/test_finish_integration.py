"""Integration test: spawn → finish creates PRs with coordination blocks."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def finish_workspace(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)

    yield workspace_with_git, mock_shell
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_finish_single_repo_no_coordination_block(finish_workspace):
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "single repo test", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    pr_url = "https://github.com/org/shared/pull/99"
    call_log = []

    def mock_run(cmd, cwd, env=None):
        call_log.append(cmd)
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout=f"{pr_url}\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    mgr = StateManager(workspace / ".mothership")
    state = mgr.load()
    assert state.tasks["single-repo-test"].pr_urls["shared"] == pr_url

    # Single repo: no gh pr edit calls (no coordination block)
    edit_calls = [c for c in call_log if "gh pr edit" in c]
    assert len(edit_calls) == 0


def test_finish_multi_repo_adds_coordination(finish_workspace):
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "multi repo test", "--repos", "shared,auth-service"])
    assert result.exit_code == 0, result.output

    pr_counter = [0]
    call_log = []

    def mock_run(cmd, cwd, env=None):
        call_log.append(cmd)
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            pr_counter[0] += 1
            return ShellResult(returncode=0, stdout=f"https://github.com/org/repo/pull/{pr_counter[0]}\n", stderr="")
        if "gh pr view" in cmd:
            return ShellResult(returncode=0, stdout="original body", stderr="")
        if "gh pr edit" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    # Verify 2 PRs created
    create_calls = [c for c in call_log if "gh pr create" in c]
    assert len(create_calls) == 2

    # Verify coordination blocks added (gh pr edit called for each)
    edit_calls = [c for c in call_log if "gh pr edit" in c]
    assert len(edit_calls) == 2


def test_finish_idempotent_rerun(finish_workspace):
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "idempotent test", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    # First finish
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        ShellResult(returncode=0, stdout="Logged in", stderr="") if "gh auth" in cmd
        else ShellResult(returncode=0, stdout="", stderr="") if "git push" in cmd
        else ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="") if "gh pr create" in cmd
        else ShellResult(returncode=0, stdout="", stderr="")
    )

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    # Second finish — should skip existing PR
    call_log = []
    original_side_effect = mock_shell.run.side_effect
    def tracking_run(cmd, cwd, env=None):
        call_log.append(cmd)
        if "gh auth" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = tracking_run

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    # No gh pr create on second run
    create_calls = [c for c in call_log if "gh pr create" in c]
    assert len(create_calls) == 0
