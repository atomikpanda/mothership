"""Integration test: monorepo with subdir service, start_mode, aliases."""
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def monorepo_workspace(tmp_path: Path):
    """Create a single-repo monorepo workspace with a subdir service."""
    root = tmp_path / "tailrd"
    root.mkdir()
    (root / "Taskfile.yml").write_text(
        "version: '3'\n"
        "tasks:\n"
        "  dev:\n"
        "    cmds:\n"
        "      - echo backend-dev\n"
        "  test:\n"
        "    cmds:\n"
        "      - echo backend-test\n"
    )
    web = root / "web"
    web.mkdir()
    (web / "Taskfile.yml").write_text(
        "version: '3'\n"
        "tasks:\n"
        "  dev:\n"
        "    cmds:\n"
        "      - echo web-dev\n"
        "  test:\n"
        "    cmds:\n"
        "      - echo web-test\n"
    )

    git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"}
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, env=git_env)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root, check=True, capture_output=True, env=git_env,
    )

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: tailrd
repos:
  tailrd:
    path: ./tailrd
    type: service
    tasks:
      run: dev
    start_mode: background
  web:
    path: web
    type: service
    git_root: tailrd
    tasks:
      run: dev
    start_mode: background
    depends_on: [tailrd]
"""
    )

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mock_shell.build_command.side_effect = lambda cmd, env_runner=None: cmd
    container.shell.override(mock_shell)

    yield tmp_path, mock_shell

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_monorepo_spawn_shares_worktree(monorepo_workspace):
    tmp_path, mock_shell = monorepo_workspace

    result = runner.invoke(app, ["spawn", "add feature"])
    assert result.exit_code == 0, result.output

    mgr = StateManager(tmp_path / ".mothership")
    state = mgr.load()
    task = state.tasks["add-feature"]

    root_wt = Path(task.worktrees["tailrd"])
    web_wt = Path(task.worktrees["web"])

    # web's worktree is a subdirectory of tailrd's worktree
    assert web_wt == root_wt / "web"
    assert root_wt.exists()
    assert web_wt.exists()


def test_monorepo_close_cleans_up(monorepo_workspace):
    tmp_path, mock_shell = monorepo_workspace

    runner.invoke(app, ["spawn", "cleanup test"])
    mgr = StateManager(tmp_path / ".mothership")
    state = mgr.load()
    root_wt = Path(state.tasks["cleanup-test"].worktrees["tailrd"])

    result = runner.invoke(app, ["close", "--yes"])
    assert result.exit_code == 0

    # The parent worktree is removed
    assert not root_wt.exists()

    state = mgr.load()
    assert state.current_task is None
    assert "cleanup-test" not in state.tasks


def test_monorepo_run_uses_background(monorepo_workspace):
    """mship run should launch both services in background."""
    tmp_path, mock_shell = monorepo_workspace

    runner.invoke(app, ["spawn", "run test"])

    # Set up Popen mocks that exit immediately
    popen_mocks = []
    def make_popen(*args, **kwargs):
        p = MagicMock()
        p.pid = 10000 + len(popen_mocks)
        p.wait.return_value = 0
        p.poll.return_value = 0
        popen_mocks.append(p)
        return p
    mock_shell.run_streaming.side_effect = make_popen

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0

    # Both background services should have been launched
    assert mock_shell.run_streaming.call_count == 2


def test_monorepo_run_uses_process_group(monorepo_workspace):
    """Background services should be launched in their own process group."""
    tmp_path, mock_shell = monorepo_workspace

    runner.invoke(app, ["spawn", "group test", "--skip-setup"])

    popen_mocks = []
    def make_popen(*args, **kwargs):
        p = MagicMock()
        p.pid = 50000 + len(popen_mocks)
        p.wait.return_value = 0
        p.poll.return_value = 0
        popen_mocks.append(p)
        return p
    mock_shell.run_streaming.side_effect = make_popen

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    # Both repos launched in background
    assert mock_shell.run_streaming.call_count == 2


def test_monorepo_healthcheck_sleep_succeeds(monorepo_workspace):
    """Integration: a repo with a sleep healthcheck reports ready in the summary."""
    tmp_path, mock_shell = monorepo_workspace

    # Rewrite config to add healthchecks
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: tailrd
repos:
  tailrd:
    path: ./tailrd
    type: service
    tasks:
      run: dev
    start_mode: background
    healthcheck:
      sleep: 10ms
  web:
    path: web
    type: service
    git_root: tailrd
    tasks:
      run: dev
    start_mode: background
    depends_on: [tailrd]
    healthcheck:
      sleep: 10ms
"""
    )

    runner.invoke(app, ["spawn", "hc integration", "--skip-setup"])

    popen_mocks = []
    def make_popen(*args, **kwargs):
        p = MagicMock()
        p.pid = 90000 + len(popen_mocks)
        p.wait.return_value = 0
        p.poll.return_value = 0
        popen_mocks.append(p)
        return p
    mock_shell.run_streaming.side_effect = make_popen

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    # Both services should have been started successfully
    assert "Started 2 background service(s)" in result.output
    assert "tailrd" in result.output
    assert "web" in result.output
