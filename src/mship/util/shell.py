import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ShellResult:
    returncode: int
    stdout: str
    stderr: str


class ShellCancelled(Exception):
    """A cancellable shell command was stopped before it completed."""


class ShellCancellationUnsupported(RuntimeError):
    """This host cannot safely create and observe a cancellation process group."""


_CANCELLATION_CHECK_INTERVAL = 0.05
_TERMINATION_GRACE_SECONDS = 1.0
_PROC_ROOT = "/proc"

_TOOL_RUNTIME_ENVIRONMENT = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "USER",
    "LOGNAME",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "ANDROID_HOME",
    "ANDROID_SDK_ROOT",
    "ANDROID_USER_HOME",
    "ANDROID_AVD_HOME",
    "JAVA_HOME",
    "GRADLE_USER_HOME",
    "DEVELOPER_DIR",
    "SDKROOT",
)


def tool_runtime_environment() -> dict[str, str]:
    """Select host toolchain variables, never workspace identity or credentials."""
    environment = {
        key: value
        for key in _TOOL_RUNTIME_ENVIRONMENT
        if (value := os.environ.get(key)) is not None
    }
    environment.setdefault("PATH", os.defpath)
    if "HOME" not in environment:
        environment["HOME"] = str(Path.home())
    environment.setdefault("LANG", "C.UTF-8")
    return environment


def _has_owned_process_group(proc: subprocess.Popen) -> bool:
    pid = getattr(proc, "pid", None)
    if os.name == "nt" or not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "darwin":
        return _darwin_group_has_executable_member(pid)
    return (
        _linux_group_has_executable_member(pid)
        if sys.platform.startswith("linux")
        else _has_owned_process_group_id(pid)
    )


def _read_linux_process_status(process_dir: Path) -> tuple[bytes, int]:
    stat = (process_dir / "stat").read_bytes()
    closing_paren = stat.rfind(b")")
    if closing_paren < 0:
        raise ValueError("process stat has no command boundary")
    fields = stat[closing_paren + 2 :].split()
    return fields[0], int(fields[2])


def ensure_cancellable_shell_supported() -> None:
    """Fail before spawn unless an owned cancellation group is observable."""
    if os.name != "posix":
        platform = "Windows" if os.name == "nt" else os.name
        raise ShellCancellationUnsupported(
            "cancellable shell execution requires POSIX process groups; "
            f"{platform} is unsupported"
        )
    if not all(
        hasattr(os, name)
        for name in (
            "waitid",
            "WNOWAIT",
            "WEXITED",
            "WNOHANG",
            "O_DIRECTORY",
            "O_NOFOLLOW",
            "fchdir",
        )
    ):
        raise ShellCancellationUnsupported(
            "cancellable shell execution requires non-reaping child observation"
        )
    if not sys.platform.startswith("linux"):
        return

    try:
        entries = os.scandir(_PROC_ROOT)
    except OSError as exc:
        raise ShellCancellationUnsupported(
            "cancellable shell execution requires a readable Linux "
            f"process-status filesystem at {_PROC_ROOT}"
        ) from exc

    last_error: Exception | None = None
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                _read_linux_process_status(Path(entry.path))
            except FileNotFoundError:
                continue
            except (OSError, IndexError, ValueError) as exc:
                last_error = exc
                continue
            return

    raise ShellCancellationUnsupported(
        "cancellable shell execution requires readable, parseable numeric "
        f"process status entries at {_PROC_ROOT}"
    ) from last_error


def _linux_group_has_executable_member(process_group: int) -> bool:
    found_member = False
    try:
        entries = os.scandir(_PROC_ROOT)
    except OSError:
        return _has_owned_process_group_id(process_group)

    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                state, member_group = _read_linux_process_status(Path(entry.path))
            except FileNotFoundError:
                continue
            except OSError, IndexError, ValueError:
                try:
                    member_group = os.getpgid(int(entry.name))
                except ProcessLookupError:
                    continue
                except OSError:
                    return _has_owned_process_group_id(process_group)
                if member_group == process_group:
                    return True
                continue
            if member_group == process_group:
                found_member = True
                if state not in {b"X", b"Z", b"x"}:
                    return True

    return not found_member and _has_owned_process_group_id(process_group)


def _darwin_group_has_executable_member(process_group: int) -> bool:
    """Inspect group members without reaping the leader that pins its identity."""
    result = subprocess.run(
        ["/bin/ps", "-axo", "pgid=,stat="],
        capture_output=True,
        check=True,
        timeout=2,
    )
    found_member = False
    for line in result.stdout.splitlines():
        group, state = line.split()
        if int(group) == process_group:
            found_member = True
            if not state.startswith(b"Z"):
                return True
    return not found_member and _has_owned_process_group_id(process_group)


def _has_owned_process_group_id(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# A trusted bootstrap avoids preexec_fn in the threaded daemon. The private,
# unlinked control file preserves the exact environment despite interpreter
# startup locale changes. The error FD closes on exec, just like Popen's own
# exec-error handshake. Neither argv nor environment is placed in public logs.
_FD_CWD_BOOTSTRAP = """
import json, os, sys
error_fd = int(sys.argv[3])
os.set_inheritable(error_fd, False)
try:
    with os.fdopen(int(sys.argv[1]), "r", encoding="utf-8") as control:
        payload = json.load(control)
    directory_fd = int(sys.argv[2])
    os.fchdir(directory_fd)
    os.close(directory_fd)
    os.execvpe(payload["argv"][0], payload["argv"], payload["env"])
except BaseException as exc:
    os.write(error_fd, str(getattr(exc, "errno", None) or 5).encode("ascii"))
    os._exit(255)
"""


def _spawn_in_directory(
    args: Sequence[str],
    directory_fd: int,
    env: Mapping[str, str],
) -> subprocess.Popen[bytes]:
    """Transfer a pinned cwd through exec without changing daemon-global cwd."""
    error_read, error_write = os.pipe()
    proc: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as control:
            json.dump({"argv": list(args), "env": dict(env)}, control)
            control.flush()
            control.seek(0)
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    _FD_CWD_BOOTSTRAP,
                    str(control.fileno()),
                    str(directory_fd),
                    str(error_write),
                ],
                env={},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
                pass_fds=(control.fileno(), directory_fd, error_write),
            )
            os.close(error_write)
            error_write = -1
            failure = os.read(error_read, 64)
            if failure:
                proc.wait()
                error_number = int(failure)
                raise OSError(error_number, os.strerror(error_number))
            return proc
    except BaseException:
        if proc is not None:
            if proc.returncode is None:
                _stop_and_reap(proc, force=True)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        raise
    finally:
        os.close(error_read)
        if error_write >= 0:
            os.close(error_write)


def _owned_process_exited(proc: subprocess.Popen) -> bool:
    """Observe exit without freeing the leader PID for an unrelated group."""
    if proc.returncode is not None:
        raise RuntimeError("owned process leader was already reaped")
    try:
        return (
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            is not None
        )
    except ChildProcessError as exc:
        raise RuntimeError("owned process identity is no longer verifiable") from exc


def _signal_owned_process(proc: subprocess.Popen, *, force: bool = False) -> None:
    pid = getattr(proc, "pid", None)
    try:
        if os.name != "nt" and isinstance(pid, int) and pid > 0:
            # A waitable leader pins its PID until the last group signal.
            # Never recover ownership from an already-reaped numeric PGID.
            exited = _owned_process_exited(proc)
            # Darwin returns EPERM when a group contains only zombies.
            # Keep the waitable leader pinned until the last group signal.
            if (
                exited
                and sys.platform == "darwin"
                and not _darwin_group_has_executable_member(pid)
            ):
                return
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
            return
        if proc.poll() is not None:
            return
        proc.kill() if force else proc.terminate()
    except ProcessLookupError:
        pass


def _reap_owned_process_leader(proc: subprocess.Popen) -> None:
    proc.wait(timeout=_TERMINATION_GRACE_SECONDS)


def _wait_for_owned_process_group_quiescence(proc: subprocess.Popen) -> None:
    pid = getattr(proc, "pid", None)
    if os.name == "nt" or not isinstance(pid, int) or pid <= 0:
        return
    deadline = time.monotonic() + 2 * _TERMINATION_GRACE_SECONDS
    while (
        _linux_group_has_executable_member(pid)
        if sys.platform.startswith("linux")
        else _darwin_group_has_executable_member(pid)
        if sys.platform == "darwin"
        else _has_owned_process_group_id(pid)
    ):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("owned process-group cleanup could not be verified")
        time.sleep(min(_CANCELLATION_CHECK_INTERVAL, remaining))


def _terminate_owned_process_group(
    proc: subprocess.Popen,
    *,
    force: bool = False,
) -> None:
    """Stop members that remain in the spawned process group.

    Descendants that create another session or process group are outside this
    selected process-group cancellation contract.
    """
    _signal_owned_process(proc, force=force)
    if os.name == "nt" or not _has_owned_process_group(proc):
        _reap_owned_process_leader(proc)
        return

    if force:
        _reap_owned_process_leader(proc)
        _wait_for_owned_process_group_quiescence(proc)
        return

    deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
    while _has_owned_process_group(proc):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _signal_owned_process(proc, force=True)
            _reap_owned_process_leader(proc)
            _wait_for_owned_process_group_quiescence(proc)
            return
        time.sleep(min(_CANCELLATION_CHECK_INTERVAL, remaining))
    _reap_owned_process_leader(proc)


def _stop_and_reap(
    proc: subprocess.Popen,
    *,
    force: bool = False,
) -> tuple[str, str]:
    """Stop the owned process group and reap its original leader."""
    _terminate_owned_process_group(proc, force=force)
    try:
        return proc.communicate(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # Cleanup already reaped the leader: another group could now use its
        # number. An escaped descendant retaining a pipe is not ours to signal.
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        raise


class ShellRunner:
    """Wraps subprocess execution with optional env_runner prefixing."""

    def build_command(self, command: str, env_runner: str | None = None) -> str:
        if env_runner:
            return f"{env_runner} {command}"
        return command

    def run(
        self,
        command: str,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ShellResult:
        """Run `command` and capture output. `timeout` (seconds) raises
        `subprocess.TimeoutExpired` if the command hasn't finished by then —
        used by lifecycle hooks (core/lifecycle_hooks.py) to bound a hook's
        runtime; other callers simply don't pass it (no timeout, unchanged
        behavior)."""
        run_env = None
        if env:
            run_env = {**os.environ, **env}
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=run_env,
            timeout=timeout,
        )
        return ShellResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def run_argv(
        self,
        args: Sequence[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ShellResult:
        """Run structured arguments without a shell.

        Cancellation requires POSIX process groups. Linux additionally requires
        readable process status so cleanup can distinguish executable members
        from zombies before returning.
        """
        if cancel_event is not None:
            ensure_cancellable_shell_supported()
        run_env = None
        if env:
            run_env = {**os.environ, **env}
        if cancel_event is None:
            result = subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                env=run_env,
                timeout=timeout,
            )
            return ShellResult(
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        kwargs = {
            "cwd": cwd,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "env": run_env,
            "start_new_session": True,
        }
        proc = subprocess.Popen(args, **kwargs)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancel_event.is_set():
                _stop_and_reap(proc)
                raise ShellCancelled(f"shell command cancelled: {args!r}")

            wait_timeout = _CANCELLATION_CHECK_INTERVAL
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stdout, stderr = _stop_and_reap(proc, force=True)
                    raise subprocess.TimeoutExpired(
                        args,
                        timeout,
                        output=stdout,
                        stderr=stderr,
                    )
                wait_timeout = min(wait_timeout, remaining)
            try:
                stdout, stderr = proc.communicate(timeout=wait_timeout)
            except subprocess.TimeoutExpired:
                continue
            return ShellResult(
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )

    def spawn_argv(
        self,
        args: Sequence[str],
        cwd: Path | int,
        env: Mapping[str, str],
    ) -> subprocess.Popen[bytes]:
        """Start a binary argv operation in a newly owned process session.

        ``env`` is the complete server-selected environment.  Unlike the
        convenience runners above, it is deliberately not merged with this
        process's ambient environment.

        An integer cwd is a borrowed directory descriptor. It is transferred
        through a trusted exec bootstrap, never resolved back to a pathname.
        """
        if isinstance(cwd, int):
            return _spawn_in_directory(args, cwd, env)
        kwargs: dict[str, object] = {
            "cwd": cwd,
            "env": dict(env),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": False,
            "bufsize": 0,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(args, **kwargs)

    def run_task(
        self,
        task_name: str,
        actual_task_name: str,
        cwd: Path,
        env_runner: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ShellResult:
        command = self.build_command(f"task {actual_task_name}", env_runner)
        return self.run(command, cwd, env=env)

    def run_streaming(
        self,
        command: str,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen:
        """Run a command with stdout/stderr streaming (for logs, run).

        Launches the subprocess in its own process group. Cancellation can
        signal processes that remain in that spawned PGID; descendants that
        create another session or process group are outside this contract.
        """
        run_env = None
        if env:
            run_env = {**os.environ, **env}
        kwargs = dict(
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=run_env,
        )
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(command, **kwargs)
