"""End-to-end smoke test: spawn → phase → test → close."""
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
    # Provide sensible defaults for audit_repos git probes so spawn doesn't block.
    def _shell_run(cmd, cwd, env=None):
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
    mock_shell.run.side_effect = _shell_run
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

    # 8. Close
    result = runner.invoke(app, ["close", "--yes"])
    assert result.exit_code == 0

    # 9. Status shows no task
    result = runner.invoke(app, ["status"])
    assert "No active task" in result.output


# ---------------------------------------------------------------------------
# Audit gate integration tests
# ---------------------------------------------------------------------------

def _reset_audit_container():
    """Reset container singletons that cache state_dir-derived paths."""
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def test_spawn_blocks_when_affected_repo_is_dirty(audit_workspace):
    state_dir = audit_workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(state_dir)
    container.log_manager.reset()
    try:
        (audit_workspace / "cli" / "x.txt").write_text("x")
        result = runner.invoke(app, ["spawn", "dirty test", "--repos", "cli"])
        assert result.exit_code == 1
        assert "dirty_worktree" in result.output
    finally:
        _reset_audit_container()


def test_spawn_force_audit_bypasses_and_logs(audit_workspace):
    from mship.core.log import LogManager

    state_dir = audit_workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(state_dir)
    container.log_manager.reset()
    try:
        (audit_workspace / "cli" / "x.txt").write_text("x")
        result = runner.invoke(app, ["spawn", "force test", "--repos", "cli", "--force-audit"])
        assert result.exit_code == 0, result.output

        log_mgr = LogManager(state_dir / "logs")
        entries = log_mgr.read("force-test")
        assert any("BYPASSED AUDIT" in e.message for e in entries)
    finally:
        _reset_audit_container()
