"""End-to-end: spawn → finish-blocked → finish-bypass (#104)."""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


def _shell_run(cmd, cwd, env=None):
    """Default shell.run side-effect satisfying audit probes cleanly."""
    if "symbolic-ref" in cmd:
        return ShellResult(returncode=0, stdout="main\n", stderr="")
    if "fetch" in cmd:
        return ShellResult(returncode=0, stdout="", stderr="")
    if "ls-remote" in cmd:
        return ShellResult(returncode=0, stdout="abc123\trefs/heads/main\n", stderr="")
    if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
        return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
    if "rev-list --count" in cmd:
        return ShellResult(returncode=0, stdout="0\n", stderr="")
    if "status --porcelain" in cmd:
        return ShellResult(returncode=0, stdout="", stderr="")
    if "worktree list" in cmd:
        return ShellResult(returncode=0, stdout="worktree /tmp/fake\n", stderr="")
    if "gh auth status" in cmd:
        return ShellResult(returncode=0, stdout="Logged in", stderr="")
    return ShellResult(returncode=0, stdout="", stderr="")


@pytest.fixture
def dep_workspace(workspace_with_git: Path):
    """workspace_with_git wired into the container with a mocked shell."""
    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = _shell_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    yield workspace_with_git

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.shell.reset_override()


def test_dependency_flow(dep_workspace):
    workspace_root = dep_workspace

    # 1. Spawn task-a (no deps).
    r1 = runner.invoke(app, ["spawn", "--hotfix", "task a", "--slug", "a", "--skip-setup", "--force-audit"])
    assert r1.exit_code == 0, r1.output

    # 2. Spawn task-b depending on a.
    r2 = runner.invoke(
        app,
        ["spawn", "--hotfix", "task b", "--slug", "b", "--depends-on", "a", "--skip-setup", "--force-audit"],
    )
    assert r2.exit_code == 0, r2.output

    # 3. Status of b shows dependencies.blocked=True and blocked_by=["a"].
    r3 = runner.invoke(app, ["status", "--task", "b"])
    assert r3.exit_code == 0, r3.output
    data = json.loads(r3.stdout)
    deps = data["resolved_task"]["dependencies"]
    assert deps["blocked"] is True
    assert deps["blocked_by"] == ["a"]

    # 4. finish task-b is blocked by the dependency gate.
    r4 = runner.invoke(app, ["finish", "--hotfix", "--task", "b"])
    err = (r4.output or "").lower()
    assert ("upstream" in err) or ("blocked" in err), (
        f"Expected 'upstream' or 'blocked' in output; got: {r4.output!r}"
    )

    # 5. --bypass-deps clears the deps gate (finish may still fail for other reasons).
    r5 = runner.invoke(app, ["finish", "--hotfix", "--task", "b", "--bypass-deps"])
    out5 = (r5.output or "").lower()
    # The specific deps-blocked message must not appear.
    assert "finish blocked: upstream" not in out5, (
        f"Expected deps gate to be cleared with --bypass-deps; got: {r5.output!r}"
    )
