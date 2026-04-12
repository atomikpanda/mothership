from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def configured_exec_app(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="test-task",
        description="Test task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/test-task",
    )
    mgr.save(WorkspaceState(current_task="test-task", tasks={"test-task": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    yield workspace, mock_shell
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.shell.reset_override()


def test_mship_test(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["test"])
    assert result.exit_code == 0
    assert mock_shell.run_task.call_count == 2


def test_mship_test_all_flag(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="fail"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all"])
    assert mock_shell.run_task.call_count == 2


def test_mship_test_fail_fast(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="fail"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test"])
    assert mock_shell.run_task.call_count == 1


def test_mship_run(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0


def test_mship_test_no_active_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    result = runner.invoke(app, ["test"])
    assert result.exit_code != 0 or "No active task" in result.output
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_mship_test_repos_filter(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["test", "--repos", "shared"])
    assert result.exit_code == 0
    assert mock_shell.run_task.call_count == 1


def test_mship_test_unknown_repo_errors(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["test", "--repos", "nonexistent"])
    assert result.exit_code != 0 or "unknown" in result.output.lower()


def test_mship_test_tag_filter(workspace: Path):
    from mship.cli import container
    from datetime import datetime, timezone

    cfg = workspace / "mothership.yaml"
    cfg.write_text("""\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
    tags: [apple]
  auth-service:
    path: ./auth-service
    type: service
    tags: [apple, mobile]
  api-gateway:
    path: ./api-gateway
    type: service
    tags: [android]
""")
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="tag-test",
        description="Tag test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service", "api-gateway"],
        branch="feat/tag-test",
    )
    mgr.save(WorkspaceState(current_task="tag-test", tasks={"tag-test": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    result = runner.invoke(app, ["test", "--tag", "apple"])
    assert result.exit_code == 0
    assert mock_shell.run_task.call_count == 2

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_mship_run_waits_for_background_services(workspace: Path):
    """mship run should block on background services, not exit immediately."""
    from mship.cli import container as cli_container
    from datetime import datetime, timezone

    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    start_mode: background
"""
    )
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    cli_container.config_path.override(cfg)
    cli_container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="bg-test",
        description="Background test",
        phase="dev",
        created_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/bg-test",
    )
    mgr.save(WorkspaceState(current_task="bg-test", tasks={"bg-test": task}))

    # Mock shell: run_streaming returns a Popen-like object that exits immediately
    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 12345
    popen_mock.wait.return_value = 0  # exits cleanly
    popen_mock.poll.return_value = 0
    mock_shell.run_streaming.return_value = popen_mock
    mock_shell.build_command.return_value = "task run"
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0

    # Should have called wait() on the background process
    popen_mock.wait.assert_called()

    cli_container.config_path.reset_override()
    cli_container.state_dir.reset_override()
    cli_container.config.reset()
    cli_container.state_manager.reset()
    cli_container.shell.reset_override()


def test_mship_run_signals_process_group_on_failure(workspace: Path):
    """On launch failure, background processes get signaled via killpg (not terminate())."""
    from mship.cli import container as cli_container
    from datetime import datetime, timezone

    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    start_mode: background
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
"""
    )
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    cli_container.config_path.override(cfg)
    cli_container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="grp-test",
        description="Group test",
        phase="dev",
        created_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/grp-test",
    )
    mgr.save(WorkspaceState(current_task="grp-test", tasks={"grp-test": task}))

    # Mock shell: run_streaming succeeds, run_task fails on auth-service
    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 99999
    mock_shell.run_streaming.return_value = popen_mock
    mock_shell.build_command.return_value = "task run"
    mock_shell.run_task.return_value = ShellResult(returncode=1, stdout="", stderr="fail")
    cli_container.shell.override(mock_shell)

    with patch("mship.cli.exec.os") as mock_os:
        mock_os.name = "posix"
        result = runner.invoke(app, ["run"])

    # Background process should be killed via killpg, not terminate()
    assert mock_os.killpg.called
    popen_mock.terminate.assert_not_called()

    cli_container.config_path.reset_override()
    cli_container.state_dir.reset_override()
    cli_container.config.reset()
    cli_container.state_manager.reset()
    cli_container.shell.reset_override()
