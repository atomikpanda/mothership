"""Integration test: parallel tiers, tag filtering, dependency types."""
from datetime import datetime, timezone

import yaml
from typer.testing import CliRunner

from mship.cli import app
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


def test_metarepo_spawn_and_test_all(metarepo_workspace):
    workspace, mock_shell = metarepo_workspace

    result = runner.invoke(app, ["spawn", "--hotfix", "add user feed"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["test", "--task", "add-user-feed"])
    assert result.exit_code == 0, result.output
    # All 5 repos should have been tested
    assert mock_shell.run_task.call_count >= 5


def test_metarepo_test_tag_apple(metarepo_workspace):
    workspace, mock_shell = metarepo_workspace

    runner.invoke(app, ["spawn", "--hotfix", "apple only test"])
    mock_shell.run_task.reset_mock()

    result = runner.invoke(app, ["test", "--tag", "apple", "--task", "apple-only-test"])
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

    runner.invoke(app, ["spawn", "--hotfix", "repos filter test"])
    mock_shell.run_task.reset_mock()

    result = runner.invoke(app, ["test", "--repos", "backend", "--task", "repos-filter-test"])
    assert result.exit_code == 0, result.output
    assert mock_shell.run_task.call_count == 1


def test_metarepo_graph(metarepo_workspace):
    workspace, mock_shell = metarepo_workspace

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "shared-swift" in result.output
    assert "backend" in result.output
