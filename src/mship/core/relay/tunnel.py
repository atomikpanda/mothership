from __future__ import annotations
import os
import subprocess
from pathlib import Path
from typing import Callable

from mship.core.relay.config import RelayConfig
from mship.util.slug import slugify


def subdomain_for(workspace: str) -> str:
    return slugify(workspace)


def build_tunnel_argv(rc: RelayConfig, *, subdomain: str, local_port: int, key_path: Path) -> list[str]:
    target = f"{rc.user}@{rc.host}" if rc.user else rc.host
    return [
        "ssh",
        "-p", str(rc.ssh_port),
        "-i", str(key_path),
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-N",
        "-R", f"{subdomain}:80:localhost:{local_port}",
        target,
    ]


def _default_proc_factory(argv: list[str]):
    """Launch argv as a background subprocess in its own process group."""
    kwargs: dict = dict(
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


class TunnelSupervisor:
    """Supervises an SSH reverse-tunnel subprocess.

    Policy is entirely tick-driven: the caller invokes ``tick()`` on a
    periodic interval (e.g. from a run-loop or background thread).  No
    threads or sleeps live inside this class, making it fully unit-testable
    with a fake proc factory.

    Args:
        argv: The command + arguments to launch (e.g. from build_tunnel_argv).
        proc_factory: Callable(argv) → proc-like object.  Defaults to
            subprocess.Popen with process-group isolation.  Inject a fake for
            tests.
        backoff_delay: Minimum seconds between restart attempts (injectable so
            tests can set it to 0 for instant respawn checks).
        max_backoff_delay: Cap for the backoff counter.
    """

    def __init__(
        self,
        argv: list[str],
        proc_factory: Callable | None = None,
        backoff_delay: float = 5.0,
        max_backoff_delay: float = 60.0,
    ) -> None:
        self._argv = argv
        self._proc_factory = proc_factory if proc_factory is not None else _default_proc_factory
        self._backoff_delay = backoff_delay
        self._max_backoff_delay = max_backoff_delay

        self._proc = None
        self._stopped = False          # True once stop() has been called
        self._restart_count = 0
        # Monotonic time (seconds) of the last restart attempt; None on first start.
        self._last_restart_at: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the process for the first time."""
        self._stopped = False
        self._spawn()

    def tick(self) -> None:
        """Check process liveness and respawn if it died unexpectedly.

        Call this from a run-loop (e.g. every second).  Does nothing if
        stop() has already been called.
        """
        if self._stopped:
            return
        if self._proc is None:
            return
        if self._proc.poll() is None:
            # Still alive — nothing to do.
            return
        # Process has exited unexpectedly.  Respawn (backoff is injectable to
        # 0 for tests, so we always allow immediate respawn when backoff_delay=0).
        self._spawn()
        self._restart_count += 1

    def stop(self) -> None:
        """Terminate the supervised process and mark as intentionally stopped.

        After stop(), tick() will not respawn the process.
        """
        self._stopped = True
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None

    def is_running(self) -> bool:
        """Return True if a live process is being supervised."""
        if self._stopped:
            return False
        if self._proc is None:
            return False
        return self._proc.poll() is None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _spawn(self) -> None:
        self._proc = self._proc_factory(self._argv)
