"""Integration test: spawn → block → log → unblock → phase → finish --handoff."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

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

    def _audit_ok_run(cmd, cwd, env=None):
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

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _audit_ok_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_agent_resilience_lifecycle(full_workspace: Path):
    # 1. Spawn task
    result = runner.invoke(app, ["spawn", "resilience test", "--repos", "shared,auth-service"])
    assert result.exit_code == 0, result.output

    # 2. Log context
    result = runner.invoke(app, ["log", "Starting work on auth controller"])
    assert result.exit_code == 0

    # 3. Phase to dev
    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code == 0

    # 4. Block
    result = runner.invoke(app, ["block", "waiting on API key"])
    assert result.exit_code == 0

    # 5. Status shows blocked
    result = runner.invoke(app, ["status"])
    assert "BLOCKED" in result.output
    assert "waiting on API key" in result.output

    # 6. Read log — should show spawn, phase, block events
    result = runner.invoke(app, ["log"])
    assert "Task spawned" in result.output
    assert "Phase transition" in result.output
    assert "Blocked: waiting on API key" in result.output

    # 7. Unblock
    result = runner.invoke(app, ["unblock"])
    assert result.exit_code == 0

    # 8. Status no longer blocked
    result = runner.invoke(app, ["status"])
    assert "BLOCKED" not in result.output

    # 9. Generate handoff
    result = runner.invoke(app, ["finish", "--handoff"])
    assert result.exit_code == 0
    handoff_file = full_workspace / ".mothership" / "handoffs" / "resilience-test.yaml"
    assert handoff_file.exists()
    with open(handoff_file) as f:
        data = yaml.safe_load(f)
    assert data["task"] == "resilience-test"
    assert len(data["merge_order"]) == 2

    # 10. Close (cleanup; finish --handoff doesn't set finished_at, so --abandon is needed)
    result = runner.invoke(app, ["close", "--yes", "--abandon"])
    assert result.exit_code == 0
