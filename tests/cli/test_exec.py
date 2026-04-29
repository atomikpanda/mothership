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
    mgr.save(WorkspaceState(tasks={"test-task": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    # Foreground `run` now goes through run_streaming + Popen.wait() (Task 3).
    # Return a Popen-shaped mock with None PIPEs so drain threads exit
    # immediately and wait() returns a real int returncode.
    popen_mock = MagicMock()
    popen_mock.pid = 12345
    popen_mock.stdout = None
    popen_mock.stderr = None
    popen_mock.wait.return_value = 0
    popen_mock.poll.return_value = 0
    mock_shell.run_streaming.return_value = popen_mock
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
    result = runner.invoke(app, ["test", "--task", "test-task"])
    assert result.exit_code == 0
    assert mock_shell.run_task.call_count == 2


def test_mship_test_all_flag(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="fail"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all", "--task", "test-task"])
    assert mock_shell.run_task.call_count == 2


def test_mship_test_fail_fast(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="fail"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--task", "test-task"])
    assert mock_shell.run_task.call_count == 1


def test_mship_test_skips_repos_with_not_applicable_test(workspace: Path):
    """End-to-end: a repo with not_applicable: [test] is skipped, not failed.
    Iteration file records status='skip'; CLI exit code is 0; no warning. See #109.
    """
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test-platform
repos:
  shared:
    path: ./shared
    type: library
  fixtures:
    path: ./fixtures
    type: library
    not_applicable: [test]
"""
    )
    (workspace / "fixtures").mkdir(exist_ok=True)
    (workspace / "fixtures" / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  build:\n    cmds:\n      - echo build\n"
    )

    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    mgr = StateManager(state_dir)
    task = Task(
        slug="skip-task",
        description="Skip task",
        phase="dev",
        created_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        affected_repos=["shared", "fixtures"],
        branch="feat/skip-task",
    )
    mgr.save(WorkspaceState(tasks={"skip-task": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    try:
        result = runner.invoke(app, ["test", "--task", "skip-task"])
        assert result.exit_code == 0, result.output
        # Only shared had `task test` invoked — fixtures was skipped.
        assert mock_shell.run_task.call_count == 1
        # The skipped repo is recorded as `skip`, not `pass`.
        persisted = mgr.load()
        assert persisted.tasks["skip-task"].test_results["fixtures"].status == "skip"
        assert persisted.tasks["skip-task"].test_results["shared"].status == "pass"
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.shell.reset_override()


def test_mship_run(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["run", "--task", "test-task"])
    assert result.exit_code == 0


def test_mship_run_works_without_active_task(workspace: Path):
    """mship run should work even when no task is active — services are workspace-scoped."""
    from unittest.mock import MagicMock
    from mship.util.shell import ShellRunner, ShellResult

    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    # Foreground `run` now goes through run_streaming + Popen.wait() (Task 3).
    popen_mock = MagicMock()
    popen_mock.pid = 54321
    popen_mock.stdout = None
    popen_mock.stderr = None
    popen_mock.wait.return_value = 0
    popen_mock.poll.return_value = 0
    mock_shell.run_streaming.return_value = popen_mock
    container.shell.override(mock_shell)

    try:
        result = runner.invoke(app, ["run", "--repos", "shared"])
        assert result.exit_code == 0, result.output
        # Executor invoked for the filtered repo. Task 3 changed foreground
        # `run` to go through run_streaming (was run_task).
        assert mock_shell.run_streaming.called
    finally:
        container.shell.reset_override()
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_mship_test_no_active_task(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    result = runner.invoke(app, ["test"])
    assert result.exit_code != 0
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_mship_test_repos_filter(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["test", "--repos", "shared", "--task", "test-task"])
    assert result.exit_code == 0
    assert mock_shell.run_task.call_count == 1


def test_mship_test_unknown_repo_errors(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["test", "--repos", "nonexistent", "--task", "test-task"])
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
    mgr.save(WorkspaceState(tasks={"tag-test": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    result = runner.invoke(app, ["test", "--tag", "apple", "--task", "tag-test"])
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
    mgr.save(WorkspaceState(tasks={"bg-test": task}))

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
    mgr.save(WorkspaceState(tasks={"grp-test": task}))

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


def test_mship_run_shows_startup_summary_with_pids(workspace: Path):
    """Startup summary should list each background service with its PID."""
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
    start_mode: background
    depends_on: [shared]
"""
    )
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    cli_container.config_path.override(cfg)
    cli_container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="summary-test",
        description="Summary test",
        phase="dev",
        created_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/summary-test",
    )
    mgr.save(WorkspaceState(tasks={"summary-test": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    pids = [11111, 22222]
    popen_mocks = []

    def make_popen(*args, **kwargs):
        p = MagicMock()
        p.pid = pids[len(popen_mocks)]
        p.wait.return_value = 0
        p.poll.return_value = 0
        popen_mocks.append(p)
        return p

    mock_shell.run_streaming.side_effect = make_popen
    mock_shell.build_command.return_value = "task run"
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "11111" in result.output
    assert "22222" in result.output
    assert "shared" in result.output
    assert "auth-service" in result.output

    cli_container.config_path.reset_override()
    cli_container.state_dir.reset_override()
    cli_container.config.reset()
    cli_container.state_manager.reset()
    cli_container.shell.reset_override()


def test_mship_run_shows_healthcheck_in_summary(workspace):
    """Startup summary includes healthcheck status per background service."""
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
    healthcheck:
      sleep: 10ms
"""
    )
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    cli_container.config_path.override(cfg)
    cli_container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="hc-summary-test",
        description="Healthcheck summary test",
        phase="dev",
        created_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/hc-summary-test",
    )
    mgr.save(WorkspaceState(tasks={"hc-summary-test": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 33333
    popen_mock.wait.return_value = 0
    popen_mock.poll.return_value = 0
    mock_shell.run_streaming.return_value = popen_mock
    mock_shell.build_command.return_value = "task run"
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    # Should have started the background service with healthcheck
    assert "Started 1 background service(s)" in result.output
    assert "shared" in result.output

    cli_container.config_path.reset_override()
    cli_container.state_dir.reset_override()
    cli_container.config.reset()
    cli_container.state_manager.reset()
    cli_container.shell.reset_override()


def test_mship_run_kills_group_after_child_exits(workspace: Path):
    """After proc.wait() returns, the process group should be signaled to catch grandchildren."""
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
        slug="cleanup-test",
        description="Cleanup test",
        phase="dev",
        created_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/cleanup-test",
    )
    mgr.save(WorkspaceState(tasks={"cleanup-test": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 77777
    popen_mock.wait.return_value = 0
    popen_mock.poll.return_value = 0
    mock_shell.run_streaming.return_value = popen_mock
    mock_shell.build_command.return_value = "task run"
    cli_container.shell.override(mock_shell)

    with patch("mship.cli.exec.os") as mock_os:
        mock_os.name = "posix"
        result = runner.invoke(app, ["run"])

    assert result.exit_code == 0
    # killpg should have been called to catch grandchildren
    assert mock_os.killpg.called
    # Should have been called multiple times (SIGTERM then SIGKILL)
    assert mock_os.killpg.call_count >= 2

    cli_container.config_path.reset_override()
    cli_container.state_dir.reset_override()
    cli_container.config.reset()
    cli_container.state_manager.reset()
    cli_container.shell.reset_override()


# --- Helpers for test-render path surfacing (issue #37) ---


def test_relpath_returns_relative_when_cwd_is_parent(tmp_path, monkeypatch):
    from mship.cli.exec import _relpath
    (tmp_path / "a" / "b").mkdir(parents=True)
    target = tmp_path / "a" / "b" / "file.txt"
    target.write_text("")
    monkeypatch.chdir(tmp_path / "a")
    assert _relpath(str(target)) == "b/file.txt"


def test_relpath_returns_absolute_when_cwd_unrelated(tmp_path, monkeypatch):
    from mship.cli.exec import _relpath
    unrelated = tmp_path / "x"
    unrelated.mkdir()
    target = tmp_path / "y" / "file.txt"
    target.parent.mkdir()
    target.write_text("")
    monkeypatch.chdir(unrelated)
    result = _relpath(str(target))
    assert result == str(target)


def test_file_nonempty_true_for_non_empty_file(tmp_path):
    from mship.cli.exec import _file_nonempty
    f = tmp_path / "a.txt"
    f.write_text("some content")
    assert _file_nonempty(str(f)) is True


def test_file_nonempty_false_for_empty_file(tmp_path):
    from mship.cli.exec import _file_nonempty
    f = tmp_path / "empty.txt"
    f.write_text("")
    assert _file_nonempty(str(f)) is False


def test_file_nonempty_false_for_missing_file(tmp_path):
    from mship.cli.exec import _file_nonempty
    f = tmp_path / "nope.txt"
    # Do not create it.
    assert _file_nonempty(str(f)) is False


# --- Render behavior for test failures (issue #37) ---


def _force_tty(monkeypatch):
    """Force Output.is_tty to True for the duration of a test so the TTY
    render path runs instead of the JSON fallback."""
    from mship.cli.output import Output
    monkeypatch.setattr(Output, "is_tty", property(lambda self: True))


def test_test_failure_prints_stderr_path(configured_exec_app, monkeypatch):
    """mship test failure renders `stderr: <path>` under the failing repo."""
    _force_tty(monkeypatch)
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="FAILED tests/foo.py::test_x — AssertionError"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all", "--task", "test-task"])
    # Verify stderr: line is present
    assert "stderr:" in result.output, result.output
    # Verify the path contains the expected components (may be wrapped across lines)
    combined = result.output.replace('\n', '')
    assert "test-runs" in combined, result.output
    assert "last 20 lines of stderr:" in result.output, result.output


def test_test_failure_prints_stdout_path_when_non_empty(configured_exec_app, monkeypatch):
    """When stdout is non-empty on a failing repo, stdout: path line appears."""
    _force_tty(monkeypatch)
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="flutter stdout contents", stderr="framing"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all", "--task", "test-task"])
    assert "stdout:" in result.output, result.output


def test_test_failure_suppresses_stdout_path_when_empty(configured_exec_app, monkeypatch):
    """When stdout is empty on a failing repo, stdout: line is NOT emitted."""
    _force_tty(monkeypatch)
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="FAILED tests/foo.py::test_x"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all", "--task", "test-task"])
    assert "stderr:" in result.output
    assert "stdout:" not in result.output, result.output


def test_test_pass_does_not_print_paths(configured_exec_app, monkeypatch):
    """Passing repos render no stderr:/stdout: lines (control)."""
    _force_tty(monkeypatch)
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["test", "--task", "test-task"])
    assert "stderr:" not in result.output
    assert "stdout:" not in result.output


def test_test_mixed_pass_fail_only_shows_paths_on_fail(configured_exec_app, monkeypatch):
    """Pass repo is clean; fail repo shows paths."""
    _force_tty(monkeypatch)
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="FAIL"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all", "--task", "test-task"])
    # Count lines that start with "    stderr:" (the path line, not the tail preamble)
    stderr_lines = [line for line in result.output.splitlines() if line.strip().startswith("stderr:")]
    assert len(stderr_lines) == 1, result.output
    assert "pass" in result.output and "fail" in result.output


def test_test_json_output_still_contains_paths(configured_exec_app):
    """Non-TTY JSON output must still include stderr_path / stdout_path keys."""
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="err"),
        ShellResult(returncode=0, stdout="out", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all", "--task", "test-task"])
    import json as _json
    payload = _json.loads(result.output)
    repos = payload["repos"]
    for name, info in repos.items():
        assert "stderr_path" in info, f"{name} missing stderr_path"
        assert "stdout_path" in info, f"{name} missing stdout_path"


def test_test_run_journal_entry_includes_parent_during_open_debug_thread(configured_git_app: Path):
    """mship test during open debug thread enriches the `ran tests` journal
    entry with parent=<latest-hypothesis-id>. See #30."""
    runner.invoke(app, ["spawn", "test parent", "--repos", "shared", "--skip-setup"])
    # Open a debug thread with a known id.
    runner.invoke(
        app, ["debug", "hypothesis", "H1", "--id", "h1", "--task", "test-parent"],
    )

    result = runner.invoke(app, ["test", "--task", "test-parent"])
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "test-parent.md").read_text()
    assert "action=ran tests" in log or 'action="ran tests"' in log
    # The ran-tests entry must carry parent=h1.
    assert "parent=h1" in log


def test_test_run_journal_entry_no_parent_when_no_debug_thread(configured_git_app: Path):
    """Regression: without open debug thread, ran-tests entry has no parent kv."""
    runner.invoke(app, ["spawn", "plain test", "--repos", "shared", "--skip-setup"])

    result = runner.invoke(app, ["test", "--task", "plain-test"])
    assert result.exit_code == 0, result.output
    log = (configured_git_app / ".mothership" / "logs" / "plain-test.md").read_text()
    assert "parent=" not in log


def test_test_refuses_when_active_repo_is_passive(tmp_path, monkeypatch):
    """`mship test` errors if active_repo is passive."""
    from datetime import datetime, timezone
    from typer.testing import CliRunner
    from mship.cli import app, container
    from mship.core.state import StateManager, Task, WorkspaceState

    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
    )
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="dev",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/x",
            worktrees={"shared": tmp_path / "wt"},
            passive_repos={"shared"},
            active_repo="shared",
        )
    }))
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)
    container.config.reset()
    container.state_manager.reset()
    monkeypatch.chdir(tmp_path)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["test", "--task", "x"])
        assert result.exit_code != 0
        assert "passive" in (result.output or "").lower()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


# -----------------------------------------------------------------------------
# Issue #127 — dedupe repos that share a resolved path; clear error on conflict
# -----------------------------------------------------------------------------

def _shared_path_workspace(
    workspace: Path,
    *,
    infra_tasks: dict[str, str] | None = None,
    infra_not_applicable: list[str] | None = None,
    tailrd_tasks: dict[str, str] | None = None,
    tailrd_not_applicable: list[str] | None = None,
) -> Path:
    """Build a workspace with two repos sharing `path: .`. Returns workspace root."""
    import yaml as _yaml

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  test:\n    cmds:\n      - echo root\n"
    )
    repos: dict = {
        "infra": {"path": ".", "type": "service"},
        "tailrd": {"path": ".", "type": "service"},
    }
    if infra_tasks:
        repos["infra"]["tasks"] = infra_tasks
    if infra_not_applicable:
        repos["infra"]["not_applicable"] = infra_not_applicable
    if tailrd_tasks:
        repos["tailrd"]["tasks"] = tailrd_tasks
    if tailrd_not_applicable:
        repos["tailrd"]["not_applicable"] = tailrd_not_applicable
    (workspace / "mothership.yaml").write_text(
        _yaml.safe_dump({"workspace": "shared", "repos": repos})
    )
    return workspace


def _seed_state(workspace: Path, *, slug: str, repos: list[str]) -> StateManager:
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)
    mgr = StateManager(state_dir)
    task = Task(
        slug=slug,
        description="t",
        phase="dev",
        created_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
        affected_repos=repos,
        branch=f"feat/{slug}",
    )
    mgr.save(WorkspaceState(tasks={slug: task}))
    return mgr


def _override_shell() -> MagicMock:
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)
    return mock_shell


def _reset_container():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.shell.reset_override()


def test_mship_test_dedupes_repos_sharing_resolved_path(workspace: Path):
    """Two repos at `path: .` with identical (empty) `tasks.test` → run once.
    Both repos appear in the iteration record sharing the same result."""
    ws = _shared_path_workspace(workspace.parent / "ws127a")
    mgr = _seed_state(ws, slug="t", repos=["infra", "tailrd"])
    mock_shell = _override_shell()
    try:
        result = runner.invoke(app, ["test", "--task", "t"])
        assert result.exit_code == 0, result.output
        # Single invocation, not two.
        assert mock_shell.run_task.call_count == 1, (
            f"expected 1 task invocation, got {mock_shell.run_task.call_count}"
        )
        # Both repos record the same status (pass).
        persisted = mgr.load().tasks["t"]
        assert persisted.test_results["infra"].status == "pass"
        assert persisted.test_results["tailrd"].status == "pass"
        # Output names both repos sharing the run.
        assert "infra" in result.output and "tailrd" in result.output
    finally:
        _reset_container()


def test_mship_test_dedupes_repos_with_same_explicit_task(workspace: Path):
    """Two repos at `path: .`, both with `tasks.test: pytest` → run pytest once."""
    ws = workspace.parent / "ws127b"
    ws.mkdir(parents=True, exist_ok=True)
    _shared_path_workspace(
        ws, infra_tasks={"test": "pytest"}, tailrd_tasks={"test": "pytest"}
    )
    mgr = _seed_state(ws, slug="t", repos=["infra", "tailrd"])
    mock_shell = _override_shell()
    try:
        result = runner.invoke(app, ["test", "--task", "t"])
        assert result.exit_code == 0, result.output
        assert mock_shell.run_task.call_count == 1
        # The actual task name resolved is `pytest`, not the canonical `test`.
        call = mock_shell.run_task.call_args
        assert call.kwargs["actual_task_name"] == "pytest"
    finally:
        _reset_container()


def test_mship_test_errors_on_path_share_with_conflicting_task_targets(workspace: Path):
    """A repo at `path: .` with no `tasks.test` AND no `not_applicable: [test]`,
    sharing the path with a repo that *does* declare `tasks.test`, must error
    rather than silently fall through to a different default. See #127."""
    ws = workspace.parent / "ws127c"
    ws.mkdir(parents=True, exist_ok=True)
    _shared_path_workspace(ws, tailrd_tasks={"test": "pytest"})
    _seed_state(ws, slug="t", repos=["infra", "tailrd"])
    mock_shell = _override_shell()
    try:
        result = runner.invoke(app, ["test", "--task", "t"])
        assert result.exit_code != 0, result.output
        # No `task` invocation should have happened — we error before running.
        assert mock_shell.run_task.call_count == 0
        out = (result.output or "").lower()
        # Names the offending repo and the three remediation paths.
        assert "infra" in out
        assert "tasks.test" in out or "tasks: test" in out.replace("`", "")
        assert "not_applicable" in out
        assert "--repos" in out
    finally:
        _reset_container()


def test_mship_test_path_share_skips_when_partner_is_not_applicable(workspace: Path):
    """One repo opted out via `not_applicable: [test]`, the other has
    `tasks.test`. Run once for the latter; skip-record the former. No error."""
    ws = workspace.parent / "ws127d"
    ws.mkdir(parents=True, exist_ok=True)
    _shared_path_workspace(
        ws,
        infra_not_applicable=["test"],
        tailrd_tasks={"test": "pytest"},
    )
    mgr = _seed_state(ws, slug="t", repos=["infra", "tailrd"])
    mock_shell = _override_shell()
    try:
        result = runner.invoke(app, ["test", "--task", "t"])
        assert result.exit_code == 0, result.output
        assert mock_shell.run_task.call_count == 1
        persisted = mgr.load().tasks["t"]
        assert persisted.test_results["infra"].status == "skip"
        assert persisted.test_results["tailrd"].status == "pass"
    finally:
        _reset_container()
