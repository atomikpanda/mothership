from __future__ import annotations
import base64
import hashlib
import hmac
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from mship.core.relay.config import RelayConfig


def subdomain_for(workspace: str) -> str:
    """Return a DNS-label-safe slug for the given workspace name.

    Rules: lowercase; runs of non-[a-z0-9] become a single '-'; leading/
    trailing '-' stripped; capped at 63 characters (DNS label max) with any
    trailing '-' after truncation also stripped.
    """
    s = workspace.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    s = s[:63].rstrip("-")
    return s


def device_id(relay_public_key: str) -> str:
    """Stable 6-char hex id for THIS machine, from its relay public key body.

    Uses only the base64 key material (the 2nd whitespace-delimited field),
    ignoring the trailing comment, so re-reading the key gives the same id.
    """
    parts = relay_public_key.split()
    body = parts[1] if len(parts) >= 2 else relay_public_key.strip()
    return hashlib.sha256(body.encode()).hexdigest()[:6]


def opaque_slug(workspace: str, secret: bytes) -> str:
    """Opaque, DNS-label-safe slug for a workspace.

    Truncated lowercase base32 of HMAC-SHA256(secret, workspace). Deterministic
    (so the subdomain is stable), yet reveals nothing about the workspace name
    without `secret` — the relay host / DNS / network only ever see the hash.
    Recover the name with `mship relay whoami` (recompute-and-match). Base32
    yields [a-z2-7] which is a subset of the DNS-label alphabet.
    """
    digest = hmac.new(secret, workspace.encode("utf-8"), hashlib.sha256).digest()
    b32 = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return b32[:12]


def device_subdomain(workspace: str, dev_id: str, secret: bytes) -> str:
    """Per-device relay subdomain: `<opaque-slug>-<dev_id>`, DNS-label-safe.

    `dev_id` is from device_id(); the leading part is now `opaque_slug()` rather
    than the readable workspace slug, so the workspace name is no longer present
    in the subdomain. Truncated so the whole label fits the 63-char DNS limit.
    """
    suffix = f"-{dev_id}"
    base = opaque_slug(workspace, secret)[: 63 - len(suffix)]
    return f"{base}{suffix}"


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


def _default_proc_factory(argv: list[str], log_path: Path | None = None):
    """Launch argv in its own process group, capturing output to log_path
    (so failures/assigned-URL are inspectable). Falls back to DEVNULL."""
    if log_path is not None:
        out = open(log_path, "ab", buffering=0)
        kwargs: dict = dict(stdout=out, stderr=subprocess.STDOUT)
    else:
        out = None
        kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
    finally:
        if out is not None:
            out.close()  # child inherited the fd; parent's handle is now redundant (closed even if Popen raised)
    return proc


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
        clock: Callable[[], float] | None = None,
        log_path: Path | None = None,
    ) -> None:
        self._argv = argv
        self._log_path = log_path
        self._proc_factory = proc_factory if proc_factory is not None \
            else (lambda a: _default_proc_factory(a, self._log_path))
        self._backoff_delay = backoff_delay
        self._max_backoff_delay = max_backoff_delay
        self._clock = clock if clock is not None else time.monotonic

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
        self._restart_count = 0
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
        # Process has exited unexpectedly.  Check whether the backoff delay has
        # elapsed before respawning.
        delay = min(
            self._backoff_delay * (2 ** self._restart_count),
            self._max_backoff_delay,
        )
        now = self._clock()
        if self._last_restart_at is None:
            # First detected exit: record the time and wait for the backoff.
            self._last_restart_at = now
        if now - self._last_restart_at < delay:
            return
        self._last_restart_at = now
        self._restart_count += 1
        self._spawn()

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

    @property
    def restart_count(self) -> int:
        """Number of times the supervised process has been restarted."""
        return self._restart_count

    def recent_output(self, limit: int = 4000) -> str:
        """Tail of the captured ssh output (empty if no log or file not yet written)."""
        if self._log_path is None:
            return ""
        try:
            data = Path(self._log_path).read_bytes()[-limit:]
            return data.decode(errors="replace")
        except FileNotFoundError:
            return ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _spawn(self) -> None:
        self._proc = self._proc_factory(self._argv)
