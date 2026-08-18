"""The daemon's tunnel half of host registration (#471).

`TunnelSupervisor` already owns "keep an `ssh -R` child alive" and `RelayLink`
already owns "keep this host's directory entry current". `HostTunnel` is the
thin object that joins them, and it exists for the three facts neither half can
know on its own:

- **An orphan must be reaped before we dial.** A `kill -9` (or a SIGKILL after a
  systemd stop timeout) leaves the `start_new_session=True` ssh child reparented
  to init, still holding the subdomain; sish then rejects the fresh tunnel and
  the relay 404s a host that looks perfectly healthy from the daemon's side.
  `scripts/redeploy-serve.sh` sweeps exactly this class by hand today, which is
  survivable for an operator running a redeploy and is not survivable for a
  daemon that restarts unattended.
- **A respawn must re-register exactly once.** The reconnected child lands on a
  fresh sish route, so the directory entry has to be re-published — but once per
  respawn, never once per tick, or a flapping tunnel turns into a registration
  storm against the relay (AC2).
- **Only a read-back proves the subdomain is ours.** A live `ssh -R` proves we
  dialed, not that we won: a `cp -a` twin publishes the *same* `host_id` on the
  *same* subdomain, so comparing host ids is blind to precisely the clone case.
  The per-process `instance_id` read back off our own public `/health` is the
  one thing that differs — and `/health` is unauthenticated (Task 3), so the
  read-back mints no token and therefore writes nothing (AC11).

Like everything it supervises, this class is entirely tick-driven: no threads,
no sleeps, every collaborator (`clock`, `reaper`, `verify`) injected, so the
whole loop is unit-testable without a socket.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from mship.core.relay.health import probe_health

log = logging.getLogger(__name__)

# How often the tunnel asks its own public URL who is answering. Far cheaper
# than the registration interval it rides beside, but not free: it is a full
# round trip out to the relay and back down our own tunnel.
READBACK_INTERVAL_S = 30.0

# What ssh prints when the relay has our key on file but nobody has approved it
# yet. It is the FIRST evidence of that state — it arrives before any
# registration verdict does — which is why the log tail is worth reading.
_UNAPPROVED_SSH_MARKER = "permission denied"

# The states this reports, in the order `state()` resolves them.
STATES = ("disabled", "duplicate-identity", "awaiting-enrollment", "contended",
          "error", "online", "connecting")


@dataclass(frozen=True)
class ProcessInfo:
    """One row of the process table, as the reaper needs it."""

    pid: int
    ppid: int
    cmdline: str


def list_processes() -> list[ProcessInfo]:
    """The process table via `ps`, or empty if it cannot be read.

    Never raises: failing to enumerate processes must degrade to "reaped
    nothing", not take the daemon's tick down with it."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,args="],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except Exception as exc:
        log.debug("could not list processes for the orphan sweep: %s", exc)
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            rows.append(ProcessInfo(pid=int(parts[0]), ppid=int(parts[1]), cmdline=parts[2]))
        except ValueError:
            continue
    return rows


def reap_orphan_tunnels(
    subdomain: str,
    *,
    processes: Callable[[], Sequence[ProcessInfo]] = list_processes,
    kill: Callable[[int], None] | None = None,
) -> list[int]:
    """Kill any `ssh -R <subdomain>:…` reparented to init; return the pids killed.

    Reparenting to init is the whole test: a tunnel we own has *us* as its
    parent, so PPID 1 means the process that dialed it is gone and nothing will
    ever clean it up. Anything that merely mentions the subdomain (an operator's
    interactive `ssh`, a `grep`) is left alone — only a live reverse forward of
    OUR label can collide with us.
    """
    if kill is None:
        kill = lambda pid: os.kill(pid, 15)
    killed = []
    for proc in processes():
        if proc.ppid != 1 or "-R" not in proc.cmdline.split():
            continue
        if f"{subdomain}:" not in proc.cmdline:
            continue
        try:
            kill(proc.pid)
        except Exception as exc:  # already gone, or not ours to signal
            log.debug("could not kill orphan tunnel pid=%s: %s", proc.pid, exc)
            continue
        log.warning("killed orphaned relay tunnel pid=%s holding %s", proc.pid, subdomain)
        killed.append(proc.pid)
    return killed


class HostTunnel:
    """Dial the relay, keep the directory entry current, and prove it is ours."""

    def __init__(
        self,
        link,
        supervisor,
        *,
        clock: Callable[[], float] = time.monotonic,
        reaper: Callable[[str], list[int]] = reap_orphan_tunnels,
        verify: Callable[..., object] = probe_health,
        readback_interval: float = READBACK_INTERVAL_S,
    ) -> None:
        self._link = link
        self._supervisor = supervisor
        self._clock = clock
        self._reaper = reaper
        self._verify = verify
        self._readback_interval = readback_interval

        self._stopped = False
        self._started = False          # True once we have dialed at least once
        self._reaped = False           # swept during the current downtime
        self._online = False           # the last read-back was us
        self._ever_online = False
        self._ssh_rejected = False     # the ssh log tail says "not approved yet"
        self._contended_with: str | None = None
        self._failure: str | None = None   # this tick's own fault, if any
        self._detail: str | None = None    # the last read-back's explanation
        self._last_readback_at: float | None = None
        self._restarts_seen = supervisor.restart_count

    # -- the loop -----------------------------------------------------------

    def tick(self) -> str:
        """One pass: supervise, register, (re)dial. Returns the new state.

        Never raises. The two halves are guarded separately and deliberately: a
        registration outage must not tear down a working tunnel, and a tunnel
        that cannot respawn must not silence registration (AC7)."""
        if self._stopped:
            return self.state()
        self._failure = None
        self._guard("tunnel", self._supervise)
        self._guard("registration", self._link.tick)
        self._guard("tunnel", self._redial)
        return self.state()

    def stop(self) -> None:
        """Terminate the ssh child and stay down. Idempotent — the second call
        must not signal a pid the supervisor has already released."""
        if self._stopped:
            return
        self._stopped = True
        self._supervisor.stop()
        self._online = False

    def _guard(self, what: str, fn: Callable[[], object]) -> None:
        try:
            fn()
        except Exception as exc:
            log.exception("%s tick failed", what)
            self._failure = f"{what}: {exc}"

    def _supervise(self) -> None:
        """Respawn a dead child, read back a live one, and tell the link when a
        respawn happened — exactly once per respawn."""
        sup = self._supervisor
        if self._started:
            if sup.is_running():
                self._reaped = False
                sup.tick()
                self._read_back(self._clock())
            else:
                self._online = False
                self._sample_ssh_log()
                self._reap()
                sup.tick()          # gated by the supervisor's own backoff
        if sup.restart_count != self._restarts_seen:
            self._restarts_seen = sup.restart_count
            # The new child holds a fresh sish route; re-publish the entry now
            # rather than waiting out `REGISTER_INTERVAL_S` (AC2).
            self._link.register_soon()

    def _redial(self) -> None:
        """Act on the link's verdict: a duplicate identity must not hold a
        tunnel open (two twins on one subdomain split traffic between them),
        and everything else dials."""
        if not self._link.should_dial():
            if self._started:
                self._supervisor.stop()
                self._started = False
                self._online = False
            return
        if not self._started:
            # First dial only after the link has had its say, so a host the
            # relay already refuses as a duplicate never spawns ssh at all (AC4).
            self._reap()
            self._supervisor.start()
            self._started = True

    def _reap(self) -> None:
        """Sweep orphans once per downtime, not once per tick: this shells out to
        the process table, and a tunnel that is down for an hour would otherwise
        do it hundreds of times."""
        if self._reaped:
            return
        self._reaped = True
        self._reaper(self._link.subdomain)

    def _sample_ssh_log(self) -> None:
        """Read the ssh tail while the child is down — the only time it explains
        anything. Ignored once a read-back has ever confirmed us: the log is
        append-only, so an old rejection stays in the tail long after the key
        was approved, and from then on the relay's own verdict is authoritative.
        """
        if self._ever_online:
            return
        tail = self._supervisor.recent_output()
        self._ssh_rejected = _UNAPPROVED_SSH_MARKER in tail.lower()

    def _read_back(self, now: float) -> None:
        """Ask our own public URL who is answering on it.

        Three outcomes, and the difference between the last two is the whole
        point: a DIFFERENT `instance_id` is a live twin (terminal — a human has
        to sort it out), while no answer at all is just an unreachable relay
        (transient), and conflating them would report every network blip as a
        clone. Same split `health.py` makes between a stale token and a route
        that has not come up yet.
        """
        if (self._last_readback_at is not None
                and 0 <= now - self._last_readback_at < self._readback_interval):
            return
        self._last_readback_at = now
        probe = self._verify(self._link.public_url, "")
        if probe.error is not None:
            self._online = False
            self._detail = f"read-back failed: {probe.error}"
            return
        if not probe.ok:
            self._online = False
            self._detail = f"read-back returned HTTP {probe.status_code}"
            return
        answered = (probe.body or {}).get("instance_id")
        if not answered:
            self._online = False
            self._detail = f"read-back of {self._link.public_url} carried no instance_id"
            return
        if answered == self._link.instance_id:
            self._online = True
            self._ever_online = True
            self._ssh_rejected = False
            self._contended_with = None
            self._detail = None
            return
        # Deliberately does NOT tear the tunnel down: we may be the incumbent and
        # the twin the newcomer, and dropping our own tunnel would hand it the
        # subdomain outright.
        self._online = False
        self._contended_with = str(answered)
        self._detail = (
            f"another host answers {self._link.public_url} (instance_id "
            f"{answered}; ours is {self._link.instance_id})"
        )

    # -- what the daemon reports --------------------------------------------

    def state(self) -> str:
        """The tunnel's state, most authoritative verdict first: what the relay
        decided about our identity, then what our own read-back saw, then how
        far along the dial is."""
        if self._stopped:
            return "disabled"
        if not self._link.should_dial():
            return "duplicate-identity"
        if self._link.state == "awaiting-enrollment" or self._ssh_rejected:
            return "awaiting-enrollment"
        if self._contended_with is not None:
            return "contended"
        if self._failure is not None or self._link.state == "error":
            return "error"
        if self._online:
            return "online"
        return "connecting"

    @property
    def last_error(self) -> str | None:
        """The most specific explanation available: this tick's own fault, then
        the read-back's, then whatever the relay last told the link."""
        return self._failure or self._detail or self._link.last_error

    @property
    def subdomain(self) -> str:
        return self._link.subdomain

    @property
    def public_url(self) -> str:
        return self._link.public_url

    @property
    def restart_count(self) -> int:
        return self._supervisor.restart_count
