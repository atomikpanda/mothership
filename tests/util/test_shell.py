import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest

from mship.util.shell import ShellCancelled, ShellRunner, ShellResult


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


def test_run_accepts_timeout_and_completes_within_it():
    runner = ShellRunner()
    result = runner.run("echo hi", cwd=Path("."), timeout=5)
    assert result.returncode == 0
    assert "hi" in result.stdout


def test_run_raises_timeout_expired_when_command_exceeds_timeout():
    runner = ShellRunner()
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run("sleep 2", cwd=Path("."), timeout=0.1)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux /proc process-state contract",
)
def test_cancelled_run_kills_descendant_after_leader_exits(tmp_path):
    ready_path = tmp_path / "descendant-ready"
    child_code = """
import os
import signal
import sys
from pathlib import Path

read_fd, write_fd = os.pipe()
child_pid = os.fork()
if child_pid == 0:
    os.close(read_fd)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.close(1)
    os.close(2)
    os.write(write_fd, b"ready")
    os.close(write_fd)
    while True:
        signal.pause()

os.close(write_fd)
os.read(read_fd, 5)
os.close(read_fd)
Path(sys.argv[1]).write_text(str(child_pid))
while True:
    signal.pause()
"""
    command = " ".join(
        (
            shlex.quote(sys.executable),
            "-c",
            shlex.quote(child_code),
            shlex.quote(str(ready_path)),
        )
    )
    cancel_event = threading.Event()
    errors: list[BaseException] = []

    def run_command() -> None:
        try:
            ShellRunner().run(
                command,
                cwd=tmp_path,
                cancel_event=cancel_event,
            )
        except BaseException as exc:
            errors.append(exc)

    runner_thread = threading.Thread(target=run_command, daemon=True)
    runner_thread.start()
    deadline = time.monotonic() + 5
    while not ready_path.exists() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert ready_path.exists(), "descendant did not report readiness"

    descendant_pid = int(ready_path.read_text())
    process_group = os.getpgid(descendant_pid)
    proc_stat = Path(f"/proc/{descendant_pid}/stat")
    try:
        cancel_event.set()
        runner_thread.join(timeout=5)
        assert not runner_thread.is_alive(), "cancelled runner did not return"
        assert len(errors) == 1
        assert isinstance(errors[0], ShellCancelled)

        try:
            descendant_state = proc_stat.read_text().split()[2]
        except FileNotFoundError:
            descendant_state = None
        assert descendant_state in {None, "X", "Z"}, (
            "SIGTERM-ignoring descendant was executable when runner cleanup returned: "
            f"{descendant_state}"
        )
    finally:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
