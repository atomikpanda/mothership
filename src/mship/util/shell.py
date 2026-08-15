import os
import signal
import subprocess
import threading
import time
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


def _signal_owned_process(proc: subprocess.Popen, *, force: bool = False) -> None:
    try:
        if os.name == "nt":
            if proc.poll() is not None:
                return
            proc.kill() if force else proc.terminate()
        else:
            # Signal the group even if its leader has already exited: children
            # may still hold the captured pipes open and must be stopped too.
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


def _stop_and_reap(
    proc: subprocess.Popen,
    *,
    force: bool = False,
) -> tuple[str, str]:
    _signal_owned_process(proc, force=force)
    try:
        return proc.communicate(timeout=1)
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
        self,
        command: str,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ShellResult:
        """Run `command` and capture output. `timeout` (seconds) raises
        `subprocess.TimeoutExpired` if the command hasn't finished by then.
        When `cancel_event` is supplied, the command owns a process group that
        is terminated and reaped before `ShellCancelled` is raised. The default
        path remains `subprocess.run` for all existing callers."""
        run_env = None
        if env:
            run_env = {**os.environ, **env}
        if cancel_event is None:
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

        kwargs = {
            "shell": True,
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
        proc = subprocess.Popen(command, **kwargs)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancel_event.is_set():
                _stop_and_reap(proc)
                raise ShellCancelled(f"shell command cancelled: {command}")

            wait_timeout = _CANCELLATION_CHECK_INTERVAL
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stdout, stderr = _stop_and_reap(proc, force=True)
                    raise subprocess.TimeoutExpired(
                        command,
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
