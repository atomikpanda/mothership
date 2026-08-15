import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ShellResult:
    returncode: int
    stdout: str
    stderr: str


class ShellCancelled(Exception):
    """A cancellable shell command was stopped before it completed."""


_CANCELLATION_CHECK_INTERVAL = 0.05
_TERMINATION_GRACE_SECONDS = 1.0


def _has_owned_process_group(proc: subprocess.Popen) -> bool:
    pid = getattr(proc, "pid", None)
    if os.name == "nt" or not isinstance(pid, int) or pid <= 0:
        return False
    return _has_owned_process_group_id(pid)


def _linux_group_has_executable_member(process_group: int) -> bool:
    found_member = False
    try:
        entries = os.scandir("/proc")
    except OSError:
        return _has_owned_process_group_id(process_group)

    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                stat = (Path(entry.path) / "stat").read_bytes()
                fields = stat[stat.rfind(b")") + 2 :].split()
                state = fields[0]
                member_group = int(fields[2])
            except FileNotFoundError:
                continue
            except (OSError, IndexError, ValueError):
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


def _has_owned_process_group_id(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_owned_process(proc: subprocess.Popen, *, force: bool = False) -> None:
    pid = getattr(proc, "pid", None)
    try:
        if os.name != "nt" and isinstance(pid, int) and pid > 0:
            # The group can outlive and be reaped after its leader. Abnormal
            # cleanup still owns that group, so signal it by the original pgid.
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
            return
        if proc.poll() is not None:
            return
        proc.kill() if force else proc.terminate()
    except ProcessLookupError:
        pass


def _reap_owned_process_leader(proc: subprocess.Popen) -> None:
    wait = getattr(proc, "wait", None)
    if not callable(wait):
        return
    try:
        wait()
    except Exception:
        pass


def _wait_for_owned_process_group_quiescence(proc: subprocess.Popen) -> None:
    pid = getattr(proc, "pid", None)
    if os.name == "nt" or not isinstance(pid, int) or pid <= 0:
        return

    if sys.platform.startswith("linux"):
        while _linux_group_has_executable_member(pid):
            time.sleep(_CANCELLATION_CHECK_INTERVAL)
        return

    while _has_owned_process_group_id(pid):
        time.sleep(_CANCELLATION_CHECK_INTERVAL)


def _terminate_owned_process_group(
    proc: subprocess.Popen,
    *,
    force: bool = False,
) -> None:
    """Stop an abnormal command tree and wait until no member can execute."""
    _signal_owned_process(proc, force=force)
    if os.name == "nt" or not _has_owned_process_group(proc):
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


def _stop_and_reap(
    proc: subprocess.Popen,
    *,
    force: bool = False,
) -> tuple[str, str]:
    """Stop an owned command tree and reap its leader before returning."""
    _terminate_owned_process_group(proc, force=force)
    try:
        return proc.communicate(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_owned_process(proc, force=True)
        return proc.communicate()


class ShellRunner:
    """Wraps subprocess execution with optional env_runner prefixing."""

    def build_command(self, command: str, env_runner: str | None = None) -> str:
        if env_runner:
            return f"{env_runner} {command}"
        return command

    def run(
        self, command: str, cwd: Path, env: dict[str, str] | None = None,
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
        """Run structured arguments without a shell, with optional cancellation."""
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
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
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

        Launches the subprocess in its own process group so signal delivery
        can reach the whole tree (including grandchildren) on termination.
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
