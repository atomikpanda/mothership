from datetime import datetime, timezone

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellResult, ShellRunner


runner = CliRunner()


@pytest.fixture
def configured_doctor_app(workspace: Path):
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    (workspace / ".mothership").mkdir(exist_ok=True)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = lambda cmd, cwd, env=None, timeout=None: (
        ShellResult(returncode=0, stdout="test\nrun\nlint\nsetup\n", stderr="") if "task --list" in cmd
        else ShellResult(returncode=0, stdout="Logged in", stderr="") if "gh auth" in cmd
        else ShellResult(returncode=0, stdout="", stderr="")
    )
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    container.shell.override(mock_shell)

    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_doctor_passes(configured_doctor_app: Path):
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    # CliRunner is non-TTY: output is JSON
    import json
    data = json.loads(result.output)
    assert data["errors"] == 0


def test_doctor_shows_workspace_name(configured_doctor_app: Path):
    result = runner.invoke(app, ["doctor"])
    # CliRunner is non-TTY: confirm command runs successfully
    assert result.exit_code == 0
    import json
    data = json.loads(result.output)
    assert "checks" in data


def test_doctor_loads_config_with_require_paths_false(workspace: Path):
    """A repo whose Taskfile.yml is missing must not crash doctor's config load;
    doctor should run and report a Taskfile fail check instead (issue 366 #5)."""
    import json
    (workspace / "auth-service" / "Taskfile.yml").unlink()  # remove one Taskfile

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    (workspace / ".mothership").mkdir(exist_ok=True)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = lambda cmd, cwd, env=None, timeout=None: (
        ShellResult(returncode=0, stdout="test\nrun\nlint\nsetup\n", stderr="") if "task --list" in cmd
        else ShellResult(returncode=0, stdout="Logged in", stderr="") if "gh auth" in cmd
        else ShellResult(returncode=0, stdout="", stderr="")
    )
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    container.shell.override(mock_shell)
    try:
        result = runner.invoke(app, ["doctor"])
        # Must NOT crash on ConfigLoader's require_paths validation; a missing
        # Taskfile is reported as a doctor `fail` check, so a clean `typer.Exit(1)`
        # (SystemExit) is EXPECTED — only a ConfigLoader ValueError would be a crash.
        assert not isinstance(result.exception, ValueError), result.output
        data = json.loads(result.output)                 # valid JSON, not a traceback
        assert any(
            c["status"] == "fail" and "Taskfile" in c["message"]
            for c in data["checks"]
        ), data["checks"]
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.shell.reset_override()


def test_doctor_json_includes_config_path_and_source(workspace, monkeypatch):
    import json
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    (workspace / ".mothership").mkdir(exist_ok=True)
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = lambda cmd, cwd, env=None, timeout=None: (
        ShellResult(returncode=0, stdout="test\nrun\nlint\nsetup\n", stderr="") if "task --list" in cmd
        else ShellResult(returncode=0, stdout="Logged in", stderr="") if "gh auth" in cmd
        else ShellResult(returncode=0, stdout="", stderr="")
    )
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    container.shell.override(mock_shell)
    monkeypatch.chdir(workspace)
    try:
        result = runner.invoke(app, ["doctor"])
        data = json.loads(result.output)
        assert data["config_path"] == str((workspace / "mothership.yaml").resolve())
        assert data["config_resolution_source"] == "walk-up"
        for k in ("checks", "warnings", "errors"):  # ac10: existing keys intact
            assert k in data
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.shell.reset_override()


def test_doctor_json_keys_are_additive(configured_doctor_app, monkeypatch):
    import json
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    result = runner.invoke(app, ["doctor"])
    data = json.loads(result.output)
    for k in ("checks", "warnings", "errors"):      # pre-existing keys intact
        assert k in data, k
    assert "config_path" in data                     # new additive keys
    assert "config_resolution_source" in data
    # Each check object keeps its stable schema:
    for c in data["checks"]:
        assert set(c.keys()) == {"name", "status", "message"}


def test_doctor_bare_remote_with_following_options_reaches_task_validation(
    workspace: Path,
):
    (workspace / "mothership.yaml").write_text(
        """\
workspace: test-platform
repos:
  app:
    path: app
    type: service
"""
    )
    app_dir = workspace / "app"
    app_dir.mkdir()
    (app_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    (workspace / ".mothership").mkdir(exist_ok=True)
    try:
        result = runner.invoke(
            app,
            ["doctor", "--remote", "--task", "no-such-task", "--repo", "app"],
        )
        assert result.exit_code == 1
        assert "--task" in result.output and "active" in result.output.lower()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_doctor_trailing_bare_remote_reaches_remote_validation(
    configured_doctor_app: Path,
):
    result = runner.invoke(app, ["doctor", "--remote"])
    assert result.exit_code == 1
    assert "--repo" in result.output


def test_doctor_explicit_remote_role_reaches_run_host_validation(workspace: Path):
    (workspace / "mothership.yaml").write_text(
        """\
workspace: test-platform
run_hosts: [default]
repos:
  app:
    path: app
    type: service
"""
    )
    app_dir = workspace / "app"
    app_dir.mkdir()
    (app_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    try:
        StateManager(state_dir).save(
            WorkspaceState(
                tasks={
                    "task-1": Task(
                        slug="task-1",
                        description="remote host validation",
                        phase="dev",
                        created_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                        affected_repos=["app"],
                        worktrees={"app": app_dir},
                        branch="feat/task-1",
                    )
                }
            )
        )
        result = runner.invoke(
            app,
            ["doctor", "--remote=studio", "--task", "task-1", "--repo", "app"],
        )
        assert result.exit_code == 1
        assert "studio" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
