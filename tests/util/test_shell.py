import subprocess
from unittest.mock import MagicMock, patch
from pathlib import Path

from mship.util.shell import ShellRunner, ShellResult


def test_run_streaming_uses_start_new_session_on_unix():
    """On Unix, run_streaming should pass start_new_session=True to Popen."""
    runner = ShellRunner()
    with patch("mship.util.shell.os.name", "posix"):
        with patch("subprocess.Popen") as mock_popen:
            runner.run_streaming("sleep 1", cwd=Path("."))
            kwargs = mock_popen.call_args.kwargs
            assert kwargs.get("start_new_session") is True
            assert "creationflags" not in kwargs


def test_run_streaming_uses_new_process_group_on_windows():
    """On Windows, run_streaming should pass creationflags=CREATE_NEW_PROCESS_GROUP."""
    CREATE_NEW_PROCESS_GROUP = 0x00000200  # Windows constant, may not exist on Linux
    runner = ShellRunner()
    with patch("mship.util.shell.os.name", "nt"):
        with patch("mship.util.shell.subprocess.CREATE_NEW_PROCESS_GROUP", CREATE_NEW_PROCESS_GROUP, create=True):
            with patch("subprocess.Popen") as mock_popen:
                runner.run_streaming("sleep 1", cwd=Path("."))
                kwargs = mock_popen.call_args.kwargs
                assert kwargs.get("creationflags") == CREATE_NEW_PROCESS_GROUP
                assert "start_new_session" not in kwargs


def test_run_simple_command():
    runner = ShellRunner()
    result = runner.run("echo hello", cwd=Path("."))
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_run_captures_stderr():
    runner = ShellRunner()
    result = runner.run("echo error >&2", cwd=Path("."))
    assert "error" in result.stderr


def test_run_returns_nonzero_on_failure():
    runner = ShellRunner()
    result = runner.run("false", cwd=Path("."))
    assert result.returncode != 0


def test_build_command_no_env_runner():
    runner = ShellRunner()
    cmd = runner.build_command("task test", env_runner=None)
    assert cmd == "task test"


def test_build_command_with_env_runner():
    runner = ShellRunner()
    cmd = runner.build_command("task test", env_runner="dotenvx run --")
    assert cmd == "dotenvx run -- task test"


def test_run_with_env_runner(tmp_path):
    """`run_task` returns a ShellResult regardless of whether `task` is
    installed or whether the cwd has a Taskfile target.

    Uses `tmp_path` (no Taskfile.yml) so this test fails fast even in
    environments where `task` is on PATH. Previously used `Path(".")`,
    which in dev environments with go-task installed would invoke the
    project's `task test` target — which itself runs `uv run pytest` —
    causing infinite recursion and a hung suite. See #115.
    """
    runner = ShellRunner()
    result = runner.run_task(
        task_name="test",
        actual_task_name="test",
        cwd=tmp_path,
        env_runner=None,
    )
    assert isinstance(result, ShellResult)


def test_run_with_env_vars():
    runner = ShellRunner()
    result = runner.run(
        'echo "$UPSTREAM_SHARED"',
        cwd=Path("."),
        env={"UPSTREAM_SHARED": "/tmp/shared-wt"},
    )
    assert result.returncode == 0
    assert "/tmp/shared-wt" in result.stdout


def test_run_task_passes_env():
    runner = ShellRunner()
    result = runner.run(
        'echo "$MY_VAR"',
        cwd=Path("."),
        env={"MY_VAR": "hello"},
    )
    assert "hello" in result.stdout
