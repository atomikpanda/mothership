from __future__ import annotations
import base64
import hashlib
import hmac
import logging
import math
import os
import random
import re
import subprocess
import time
from threading import Lock
from pathlib import Path
from typing import Callable

from mship.core.relay.config import RelayConfig

log = logging.getLogger(__name__)

# Up to 20% OFF a scheduled delay (never added — see `jittered`): enough to
# de-phase a fleet that all lost the relay in the same second, small enough that
# the delay still means roughly what it says. Owned here because both retry
# loops in the reconnect path jitter with it — this supervisor and the daemon's
# `core/daemon/relay_link.py`.
BACKOFF_JITTER = 0.2


def jittered(delay: float, rng: Callable[[], float]) -> float:
    """De-phase DOWNWARD only. `host_contract.DIRECTORY_STALE_S` is derived from
    `MAX_BACKOFF_S`, so a delay jittered *above* the cap would let a healthy
    reconnecting host read as stale in the relay's directory."""
    return delay * (1 - BACKOFF_JITTER * rng())


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
        argv: The command + arguments to launch (e.g. from build_tunnel_argv),
            or a zero-argument callable returning them. Pass the callable when
            any part of the command can change between spawns: the daemon's
            subdomain moves with its host identity (#471 AC4), and a frozen
            argv would re-dial a subdomain the host no longer owns.
        proc_factory: Callable(argv) → proc-like object.  Defaults to
            subprocess.Popen with process-group isolation.  Inject a fake for
            tests.
        backoff_delay: Minimum seconds between restart attempts (injectable so
            tests can set it to 0 for instant respawn checks).
        max_backoff_delay: Cap for the backoff counter. A run that outlives it
            counts as healthy and clears the failure streak.
        rng: Source of the downward backoff jitter (injectable for tests).
    """

    def __init__(
        self,
        argv: list[str] | Callable[[], list[str]],
        proc_factory: Callable | None = None,
        backoff_delay: float = 5.0,
        max_backoff_delay: float = 60.0,
        clock: Callable[[], float] | None = None,
        log_path: Path | None = None,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._argv = argv
        self._log_path = log_path
        self._proc_factory = proc_factory if proc_factory is not None \
            else (lambda a: _default_proc_factory(a, self._log_path))
        self._backoff_delay = backoff_delay
        self._max_backoff_delay = max_backoff_delay
        self._clock = clock if clock is not None else time.monotonic
        self._rng = rng if rng is not None else random.random
        self._proc_lock = Lock()
        # DERIVED, not picked: the first exponent whose delay already exceeds the
        # cap. Clamping there is what keeps `2 ** n` from overflowing a float
        # after ~1024 restarts — a bound a CLI never reaches and an immortal
        # daemon (#471) does, at roughly 17h of flapping.
        self._max_exponent = (
            max(0, math.ceil(math.log2(max_backoff_delay / backoff_delay)))
            if backoff_delay > 0 and max_backoff_delay > 0
            else 0
        )

        self._proc = None
        self._stopped = False          # True once stop() has been called
        self._final = False            # ... and stopped for good: never spawn again
        self._restart_count = 0
        # Every process this supervisor has successfully launched. Unlike
        # `_restart_count` it only ever increases — `start()` zeroes the restart
        # counter and a healthy run resets it, so a caller that wants to know
        # "did a NEW child appear?" must ask this instead (#471 AC2).
        self._spawn_count = 0
        # Monotonic time (seconds) the current process was spawned at; a run
        # longer than the backoff cap is what ends a failure streak. None while
        # no process is known to be up, INCLUDING after a spawn that raised.
        self._spawned_at: float | None = None
        # Monotonic time (seconds) an exit was first detected at, and the delay
        # frozen for it. None while a process is (believed) alive.
        self._last_restart_at: float | None = None
        self._delay = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the process for the first time.

        Refused after a FINAL stop, and deliberately not re-armable: the caller
        that wants a tunnel again after a shutdown is a new process."""
        if self._final:
            log.warning("refusing to dial: this tunnel supervisor was stopped for good")
            return
        self._stopped = False
        self._restart_count = 0
        self._last_restart_at = None
        self._delay = 0.0
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
        now = self._clock()
        if self._last_restart_at is None:
            # First detected exit: freeze one delay for it (re-jittering it on
            # every tick would resample the gate instead of honouring it) and
            # wait the backoff out.
            if self._spawned_at is not None and now - self._spawned_at >= self._max_backoff_delay:
                # The tunnel held for longer than the worst case we would ever
                # wait, so whatever streak preceded it is over.
                self._restart_count = 0
            self._delay = jittered(self._backoff(), self._rng)
            self._last_restart_at = now
        if now - self._last_restart_at < self._delay:
            return
        self._restart_count += 1
        self._last_restart_at = None
        self._spawn()

    def stop(self, *, final: bool = False) -> None:
        """Terminate the supervised process and mark as intentionally stopped.

        After stop(), tick() will not respawn the process — but `start()` still
        may, which is what the duplicate-identity recovery in `HostTunnel`
        depends on.

        `final=True` LATCHES that shut, and nothing re-arms it. It is for
        process shutdown, where a caller already past its own checks (a tick
        running on the executor thread while this runs on the loop thread) could
        otherwise reach `start()` a moment after the child was signalled and
        fork a fresh `start_new_session=True` ssh that nothing then owns — an
        orphan holding the subdomain against the next boot (#471 AC7).
        """
        with self._proc_lock:
            self._stopped = True
            self._final = self._final or final
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            except Exception:
                pass

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

    @property
    def spawn_count(self) -> int:
        """Processes successfully launched, monotonically. The signal for "a new
        child exists" — `restart_count` is not, it resets."""
        return self._spawn_count

    def next_delay(self) -> float:
        """Seconds from the detected exit until the respawn is due (0 while the
        process is believed alive) — the jittered value actually being waited."""
        return self._delay if self._last_restart_at is not None else 0.0

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

    def _backoff(self) -> float:
        """Capped exponential delay for the current failure streak. The exponent
        is clamped as well as the product, so the multiplication itself cannot
        overflow (`5.0 * 2 ** 1024` raises `OverflowError`)."""
        return min(
            self._backoff_delay * (2 ** min(self._restart_count, self._max_exponent)),
            self._max_backoff_delay,
        )

    def _spawn(self) -> None:
        # Cleared FIRST and stamped only once the factory has actually returned:
        # a spawn that raises (log file EACCES, `ssh` not on PATH) started no
        # run at all, and recording one would let the next exit-detection read a
        # never-started process as a healthy run and wipe the failure streak —
        # sawtoothing the escalation the clamp exists to reach.
        self._spawned_at = None
        with self._proc_lock:
            if self._final:
                # Checked at the LAST moment, not only in start(): the caller
                # may have passed its own check before the latch closed.
                return
        argv = self._argv() if callable(self._argv) else self._argv
        proc = self._proc_factory(argv)
        with self._proc_lock:
            stopped_during_spawn = self._stopped
            if not stopped_during_spawn:
                self._proc = proc
                self._spawned_at = self._clock()
                self._spawn_count += 1
        if stopped_during_spawn:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            except Exception:
                pass


def host_subdomain(host_id: str, dev_id: str, secret: bytes) -> str:
    """Per-HOST relay subdomain (#471), same shape as `device_subdomain` with
    the host id in the workspace slot — so it satisfies the existing
    `tls_ask_allowed` pattern and needs no TLS/Caddy cert change."""
    return device_subdomain(host_id, dev_id, secret)
