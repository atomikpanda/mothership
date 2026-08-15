import os
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest
from mship.util import shell as shell_module
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


def test_run_argv_passes_shell_metacharacters_literally(tmp_path):
    side_effect = tmp_path / "must-not-exist"
    literal = f"value; touch {side_effect}"

    result = ShellRunner().run_argv(
        [
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1])",
            literal,
        ],
        cwd=tmp_path,
        cancel_event=threading.Event(),
    )

    assert result.returncode == 0
    assert result.stdout.strip() == literal
    assert not side_effect.exists()


def _successful_popen():
    popen = MagicMock()
    popen.return_value.communicate.return_value = ("ok\n", "")
    popen.return_value.returncode = 0
    return popen


def test_cancellable_run_argv_rejects_windows_before_spawn(monkeypatch):
    cwd = Path(".")
    popen = _successful_popen()
    monkeypatch.setattr(
        shell_module.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0x00000200,
        raising=False,
    )
    monkeypatch.setattr(shell_module.os, "name", "nt")
    monkeypatch.setattr(shell_module.subprocess, "Popen", popen)

    with pytest.raises(RuntimeError, match="Windows") as exc_info:
        ShellRunner().run_argv(
            ["command"],
            cwd=cwd,
            cancel_event=threading.Event(),
        )

    assert exc_info.type.__name__ == "ShellCancellationUnsupported"
    popen.assert_not_called()


def test_cancellable_run_argv_rejects_unreadable_linux_proc_before_spawn(
    tmp_path,
    monkeypatch,
):
    popen = _successful_popen()
    monkeypatch.setattr(shell_module.os, "name", "posix")
    monkeypatch.setattr(shell_module.sys, "platform", "linux")
    monkeypatch.setattr(
        shell_module,
        "_PROC_ROOT",
        tmp_path / "unavailable-proc",
        raising=False,
    )
    monkeypatch.setattr(shell_module.subprocess, "Popen", popen)

    with pytest.raises(RuntimeError, match="process-status") as exc_info:
        ShellRunner().run_argv(
            ["command"],
            cwd=tmp_path,
            cancel_event=threading.Event(),
        )

    assert exc_info.type.__name__ == "ShellCancellationUnsupported"
    popen.assert_not_called()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_cancellable_run_argv_accepts_supported_posix_hosts(
    tmp_path,
    monkeypatch,
    platform,
):
    proc_root = tmp_path / "proc"
    if platform == "linux":
        process_dir = proc_root / "123"
        process_dir.mkdir(parents=True)
        (process_dir / "stat").write_bytes(
            b"123 (python) S 1 123 0 0 0 0 0\n"
        )
    popen = _successful_popen()
    monkeypatch.setattr(shell_module.os, "name", "posix")
    monkeypatch.setattr(shell_module.sys, "platform", platform)
    monkeypatch.setattr(shell_module, "_PROC_ROOT", proc_root, raising=False)
    monkeypatch.setattr(shell_module.subprocess, "Popen", popen)

    result = ShellRunner().run_argv(
        ["command"],
        cwd=tmp_path,
        cancel_event=threading.Event(),
    )

    assert result.returncode == 0
    popen.assert_called_once()


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
    command = [
        sys.executable,
        "-c",
        child_code,
        str(ready_path),
    ]
    cancel_event = threading.Event()
    errors: list[BaseException] = []

    def run_command() -> None:
        try:
            ShellRunner().run_argv(
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


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_forced_group_cleanup_reaps_leader_before_absence_wait(
    monkeypatch,
    platform,
):
    killed = threading.Event()
    reaped = threading.Event()
    descendant_absent = threading.Event()
    cleanup_done = threading.Event()
    events: list[str] = []

    class _KilledLeader:
        pid = 424242

        def poll(self):
            return None

        def wait(self):
            events.append("reap")
            reaped.set()
            return -signal.SIGKILL

    proc = _KilledLeader()

    def fake_killpg(process_group, signum):
        assert process_group == proc.pid
        if signum == signal.SIGKILL:
            events.append("kill")
            killed.set()
            return
        if signum == 0:
            events.append("probe")
            if reaped.is_set() and descendant_absent.is_set():
                raise ProcessLookupError

    monkeypatch.setattr(shell_module.sys, "platform", platform)
    monkeypatch.setattr(shell_module.os, "killpg", fake_killpg)
    if platform == "linux":
        monkeypatch.setattr(
            shell_module,
            "_linux_group_has_executable_member",
            lambda process_group: shell_module._has_owned_process_group_id(
                process_group
            ),
        )

    def cleanup() -> None:
        shell_module._terminate_owned_process_group(proc)
        cleanup_done.set()

    cleanup_thread = threading.Thread(target=cleanup, daemon=True)
    cleanup_thread.start()
    try:
        assert killed.wait(timeout=2), "cleanup did not escalate to SIGKILL"
        assert reaped.wait(timeout=1), "leader was not reaped after SIGKILL"
        assert not cleanup_done.is_set(), "cleanup ignored the surviving descendant"
        descendant_absent.set()
        assert cleanup_done.wait(timeout=1), "cleanup did not observe group absence"
        assert events.index("reap") > events.index("kill")
    finally:
        reaped.set()
        descendant_absent.set()
        cleanup_thread.join(timeout=2)


def test_linux_group_scan_accepts_non_utf8_process_name(tmp_path, monkeypatch):
    process_dir = tmp_path / "123"
    process_dir.mkdir()
    (process_dir / "stat").write_bytes(
        b"123 (\xff process) S 1 424242 0 0 0 0 0\n"
    )
    real_scandir = os.scandir

    def scan_test_proc(path):
        assert path == "/proc"
        return real_scandir(tmp_path)

    monkeypatch.setattr(shell_module.os, "scandir", scan_test_proc)

    assert shell_module._linux_group_has_executable_member(424242)


def test_linux_group_scan_accepts_zombie_with_unreadable_unrelated_stat(
    tmp_path,
    monkeypatch,
):
    zombie_dir = tmp_path / "123"
    zombie_dir.mkdir()
    (zombie_dir / "stat").write_bytes(
        b"123 (zombie) Z 1 424242 0 0 0 0 0\n"
    )
    unreadable_dir = tmp_path / "456"
    unreadable_dir.mkdir()
    (unreadable_dir / "stat").write_bytes(b"malformed")
    real_scandir = os.scandir

    monkeypatch.setattr(
        shell_module.os,
        "scandir",
        lambda path: real_scandir(tmp_path),
    )
    monkeypatch.setattr(shell_module.os, "killpg", lambda group, signum: None)

    assert not shell_module._linux_group_has_executable_member(424242)
