"""Shared fixtures for tests/cli/."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.cli import container
from mship.util.shell import ShellResult, ShellRunner


def _audit_ok_run(cmd, cwd, env=None):
    """Default shell.run side_effect that satisfies audit_repos probes cleanly."""
    if "symbolic-ref" in cmd:
        return ShellResult(returncode=0, stdout="main\n", stderr="")
    if "fetch" in cmd:
        return ShellResult(returncode=0, stdout="", stderr="")
    if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
        return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
    if "rev-list --count" in cmd:
        return ShellResult(returncode=0, stdout="0\n", stderr="")
    if "status --porcelain" in cmd:
        return ShellResult(returncode=0, stdout="", stderr="")
    if "worktree list" in cmd:
        return ShellResult(returncode=0, stdout="worktree /tmp/fake\n", stderr="")
    return ShellResult(returncode=0, stdout="", stderr="")


@pytest.fixture
def configured_git_app(workspace_with_git: Path):
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _audit_ok_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.log_manager.reset()
    container.shell.reset_override()
