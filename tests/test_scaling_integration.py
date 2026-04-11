"""Integration test: parallel tiers, tag filtering, dependency types."""
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime, timezone

import pytest
import yaml
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def metarepo_workspace(tmp_path: Path):
    """Create a 5-repo metarepo-style workspace with tags and dep types."""
    for name in ["shared-swift", "backend", "ios-app", "android-app", "macos-app"]:
        d = tmp_path / name
        d.mkdir()
        (d / "Taskfile.yml").write_text(f"version: '3'\ntasks:\n  test:\n    cmds:\n      - echo {name}\n")
        subprocess.run(["git", "init", str(d)], check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=d, check=True, capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
        )

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("""\
workspace: my-platform
repos:
  shared-swift:
    path: ./shared-swift
    type: library
    tags: [apple]
  backend:
    path: ./backend
    type: service
    tags: [backend]
  ios-app:
    path: ./ios-app
    type: service
    tags: [apple, mobile]
    depends_on:
      - repo: shared-swift
        type: compile
      - repo: backend
        type: runtime
  android-app:
    path: ./android-app
    type: service
    tags: [android, mobile]
    depends_on:
      - repo: backend
        type: runtime
  macos-app:
    path: ./macos-app
    type: service
    tags: [apple, desktop]
    depends_on:
      - repo: shared-swift
        type: compile
      - repo: backend
        type: runtime
""")

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)

    yield tmp_path, mock_shell

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_metarepo_spawn_and_test_all(metarepo_workspace):
    workspace, mock_shell = metarepo_workspace

    result = runner.invoke(app, ["spawn", "add user feed"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["test"])
    assert result.exit_code == 0, result.output
    # All 5 repos should have been tested
    assert mock_shell.run_task.call_count >= 5


def test_metarepo_test_tag_apple(metarepo_workspace):
    workspace, mock_shell = metarepo_workspace

    runner.invoke(app, ["spawn", "apple only test"])
    mock_shell.run_task.reset_mock()

    result = runner.invoke(app, ["test", "--tag", "apple"])
    assert result.exit_code == 0, result.output
    # shared-swift, ios-app, macos-app = 3 repos
    repos_tested = set()
    for c in mock_shell.run_task.call_args_list:
        cwd = str(c.kwargs["cwd"])
        for name in ["shared-swift", "ios-app", "macos-app", "android-app", "backend"]:
            if name in cwd:
                repos_tested.add(name)
    assert "shared-swift" in repos_tested
    assert "ios-app" in repos_tested
    assert "macos-app" in repos_tested
    assert "android-app" not in repos_tested
    assert "backend" not in repos_tested


def test_metarepo_test_repos_filter(metarepo_workspace):
    workspace, mock_shell = metarepo_workspace

    runner.invoke(app, ["spawn", "repos filter test"])
    mock_shell.run_task.reset_mock()

    result = runner.invoke(app, ["test", "--repos", "backend"])
    assert result.exit_code == 0, result.output
    assert mock_shell.run_task.call_count == 1


def test_metarepo_graph(metarepo_workspace):
    workspace, mock_shell = metarepo_workspace

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "shared-swift" in result.output
    assert "backend" in result.output
