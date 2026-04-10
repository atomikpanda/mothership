"""End-to-end smoke test: spawn → phase → test → abort."""
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner
from unittest.mock import MagicMock

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def full_workspace(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_full_lifecycle(full_workspace: Path):
    # 1. Spawn
    result = runner.invoke(app, ["spawn", "add labels", "--repos", "shared,auth-service"])
    assert result.exit_code == 0, result.output
    assert "add-labels" in result.output

    # 2. Check status
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "add-labels" in result.output
    assert "plan" in result.output

    # 3. Transition to dev
    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code == 0

    # 4. Check status shows dev
    result = runner.invoke(app, ["status"])
    assert "dev" in result.output

    # 5. Run tests
    result = runner.invoke(app, ["test"])
    assert result.exit_code == 0

    # 6. List worktrees
    result = runner.invoke(app, ["worktrees"])
    assert result.exit_code == 0
    assert "add-labels" in result.output

    # 7. Show graph
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "shared" in result.output

    # 8. Abort
    result = runner.invoke(app, ["abort", "--yes"])
    assert result.exit_code == 0

    # 9. Status shows no task
    result = runner.invoke(app, ["status"])
    assert "No active task" in result.output
