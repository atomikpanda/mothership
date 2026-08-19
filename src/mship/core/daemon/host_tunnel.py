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
import signal
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

# How often the daemon's loop calls `tick()`. Not a schedule of its own: every
# collaborator this class drives already owns one (the link's registration
# interval, the supervisor's respawn backoff, `READBACK_INTERVAL_S`), so this is
# only the resolution at which they are asked — the "every second" cadence
# `TunnelSupervisor` documents for its own tick.
TICK_INTERVAL_S = 1.0

# Bound on the orphan sweep's shell-out. Named because the daemon's shutdown
# derives its tunnel-join bound from it (`core/daemon/run.py`) — a tick must
# not be able to outlast the wait that exists to keep it from orphaning ssh.
PROCESS_LIST_TIMEOUT_S = 10.0
# Discovery, pre-TERM revalidation, and pre-KILL revalidation. Each phase takes
# one bounded process-table snapshot regardless of how many orphans it contains.
MAX_PROCESS_LIST_CALLS_PER_REAP = 3


# One shared grace period for every matching orphan: signal all of them, wait
# together, then escalate survivors. The daemon's shutdown join bound includes
# both this TERM wait and the post-KILL confirmation wait.
ORPHAN_EXIT_TIMEOUT_S = 2.0
_ORPHAN_EXIT_POLL_S = 0.05


# The states this reports, in the order `state()` resolves them.
STATES = (
    "disabled",
    "duplicate-identity",
    "awaiting-enrollment",
    "contended",
    "error",
    "online",
    "connecting",
)


@dataclass(frozen=True)
class ProcessInfo:
    """One process-table row, including its creation identity."""

    pid: int
    ppid: int
    cmdline: str
    started: str


def list_processes() -> list[ProcessInfo]:
    """Read the process table with a stable creation identity per PID.

    Failure is loud: dialing without knowing whether an orphan still owns the
    reverse forward recreates the collision this sweep exists to prevent.
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,lstart=,args="],
            capture_output=True,
            text=True,
            timeout=PROCESS_LIST_TIMEOUT_S,
            check=True,
        ).stdout
    except Exception as exc:
        raise RuntimeError("could not list processes for the orphan sweep") from exc
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) != 8:
            continue
        try:
            rows.append(
                ProcessInfo(
                    pid=int(parts[0]),
                    ppid=int(parts[1]),
                    started=" ".join(parts[2:7]),
                    cmdline=parts[7],
                )
            )
        except ValueError:
            continue
    return rows


def _forward_label(cmdline: str) -> str | None:
    """The subdomain an `ssh … -R <label>:<port>:…` command forwards to.

    Parsed as a token, never matched as a substring: `-R xyz-<oursub>:80:…` is
    somebody else's label that merely ends with ours, and signalling it would
    take down an unrelated host's tunnel.
    """
    tokens = cmdline.split()
    try:
        spec = tokens[tokens.index("-R") + 1]
    except (ValueError, IndexError):
        return None
    return spec.split(":")[0]


def _process_exists(pid: int) -> bool:
    """Whether pid is still live (a zombie has already released its sockets)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if not os.path.isdir("/proc"):
        return True
    try:
        with open(f"/proc/{pid}/stat") as stat_file:
            stat = stat_file.read()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    closing_paren = stat.rfind(")")
    return closing_paren < 0 or not stat[closing_paren + 2 :].startswith("Z")


def reap_orphan_tunnels(
    subdomain: str,
    *,
    processes: Callable[[], Sequence[ProcessInfo]] = list_processes,
    kill: Callable[[int], None] | None = None,
    force_kill: Callable[[int], None] | None = None,
    pidfd_open: Callable[[int], int] | None = None,
    pidfd_signal: Callable[[int, int], None] | None = None,
    close_pidfd: Callable[[int], None] = os.close,
    process_exists: Callable[[int], bool] = _process_exists,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[int]:
    """Terminate `ssh -R <subdomain>:…` processes reparented to init.

    Reparenting to init is the whole test: a tunnel we own has *us* as its
    parent, so PPID 1 means the process that dialed it is gone and nothing will
    ever clean it up. Anything that merely mentions the subdomain (an operator's
    interactive `ssh`, a `grep`, a host whose label ends with ours) is left
    alone — only a live reverse forward of OUR exact label can collide with us.

    Every match receives SIGTERM before one shared bounded wait. Survivors then
    receive SIGKILL and another bounded wait, so the caller cannot redial while
    an old process still owns the reverse forward. Returns the pids that
    accepted the initial signal.
    """

    def wait_for_exit(processes: list[ProcessInfo]) -> list[ProcessInfo]:
        deadline = clock() + ORPHAN_EXIT_TIMEOUT_S
        live = [process for process in processes if process_exists(process.pid)]
        while live:
            remaining = deadline - clock()
            if remaining <= 0:
                return live
            sleep(min(_ORPHAN_EXIT_POLL_S, remaining))
            live = [process for process in live if process_exists(process.pid)]
        return []

    def same_process(expected: ProcessInfo, current: ProcessInfo | None) -> bool:
        return (
            current is not None
            and current.started == expected.started
            and current.ppid == 1
            and _forward_label(current.cmdline) == subdomain
        )

    pidfd_opener = pidfd_open or getattr(os, "pidfd_open", None)
    native_pidfd_sender = getattr(signal, "pidfd_send_signal", None)
    pidfd_sender = pidfd_signal or (
        (lambda fd, sig: native_pidfd_sender(fd, sig, None, 0))
        if native_pidfd_sender is not None
        else None
    )

    def signal_phase(
        candidates: list[ProcessInfo],
        sig: int,
        injected_signal: Callable[[int], None] | None,
    ) -> list[ProcessInfo]:
        """Revalidate and signal a whole phase from one bounded snapshot."""
        if not candidates:
            return []
        use_pidfds = (
            injected_signal is None
            and pidfd_opener is not None
            and pidfd_sender is not None
        )
        opened: dict[int, int] = {}
        try:
            if use_pidfds:
                for proc in candidates:
                    try:
                        opened[proc.pid] = pidfd_opener(proc.pid)
                    except ProcessLookupError:
                        continue
                    except Exception as exc:
                        raise RuntimeError(
                            "atomic pidfd signaling is unavailable"
                        ) from exc

            current_by_pid = {proc.pid: proc for proc in processes()}
            accepted = []
            for proc in candidates:
                if use_pidfds and proc.pid not in opened:
                    continue
                current = current_by_pid.get(proc.pid)
                if current is None:
                    if process_exists(proc.pid):
                        raise RuntimeError(
                            f"could not revalidate orphan tunnel pid {proc.pid}"
                        )
                    continue
                if not same_process(proc, current):
                    continue
                try:
                    if use_pidfds:
                        pidfd_sender(opened[proc.pid], sig)
                    elif injected_signal is not None:
                        injected_signal(proc.pid)
                    else:
                        # macOS has no pidfds. The shared phase snapshot narrows
                        # the unavoidable PID-reuse race before this signal.
                        os.kill(proc.pid, sig)
                except ProcessLookupError:
                    continue
                except Exception as exc:
                    if process_exists(proc.pid):
                        action = "force-kill" if sig == signal.SIGKILL else "signal"
                        raise RuntimeError(
                            f"could not {action} orphan tunnel pid {proc.pid}"
                        ) from exc
                    continue
                accepted.append(proc)
            return accepted
        finally:
            for fd in opened.values():
                close_pidfd(fd)

    candidates = [
        proc
        for proc in processes()
        if proc.ppid == 1 and _forward_label(proc.cmdline) == subdomain
    ]
    identities = signal_phase(candidates, signal.SIGTERM, kill)
    for proc in identities:
        log.warning(
            "signalled orphaned relay tunnel pid=%s holding %s", proc.pid, subdomain
        )
    signalled = [proc.pid for proc in identities]

    survivors = wait_for_exit(identities)
    force_signalled = signal_phase(survivors, signal.SIGKILL, force_kill)
    for proc in force_signalled:
        log.warning(
            "force-killed orphaned relay tunnel pid=%s holding %s",
            proc.pid,
            subdomain,
        )
    still_live = wait_for_exit(force_signalled)
    if still_live:
        raise RuntimeError(
            "orphaned relay tunnel pids still live after SIGKILL: "
            + ", ".join(str(proc.pid) for proc in still_live)
        )
    return signalled


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
        self._started = False  # True after the first dial attempt
        self._reaped = False  # swept during the current downtime
        self._online = False  # the last read-back was us
        self._contended_with: str | None = None
        self._failure: str | None = None  # persists until the failed operation recovers
        self._detail: str | None = None  # the last read-back's explanation
        self._last_readback_at: float | None = None
        # Monotonic: `restart_count` is NOT a respawn signal — a healthy run
        # resets it and `start()` zeroes it, so diffing it would read a *drop*
        # as a respawn and re-register while no child exists at all (AC2).
        self._spawns_seen = supervisor.spawn_count
        self._publish()

    # -- the loop -----------------------------------------------------------

    def tick(self) -> str:
        """One pass: supervise, register, (re)dial. Returns the new state.

        Never raises. The two halves are guarded separately and deliberately: a
        registration outage must not tear down a working tunnel, and a tunnel
        that cannot respawn must not silence registration (AC7)."""
        if self._stopped:
            return self.state()
        self._guard("tunnel", self._supervise)
        self._guard("registration", self._link.tick)
        self._guard("tunnel", self._redial)
        self._publish()
        return self.state()

    def stop(self) -> None:
        """Terminate the ssh child and stay down, for good. Idempotent — the
        second call must not signal a pid the supervisor has already released.

        `final=True` because this is the shutdown path: a tick still in flight
        on another thread must not be able to dial a replacement behind us."""
        if self._stopped:
            return
        self._stopped = True
        self._supervisor.stop(final=True)
        self._online = False
        self._publish()

    def _guard(self, what: str, fn: Callable[[], object]) -> None:
        try:
            fn()
        except Exception as exc:
            log.exception("%s tick failed", what)
            self._failure = f"{what}: {exc}"
        else:
            if (
                what == "registration"
                and self._failure is not None
                and self._failure.startswith("registration:")
            ):
                self._failure = None

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
                self._reap()
                sup.tick()  # gated by the supervisor's own backoff
        if sup.spawn_count != self._spawns_seen:
            self._spawns_seen = sup.spawn_count
            # A new child is up, holding a fresh sish route; re-publish the entry
            # now rather than waiting out `REGISTER_INTERVAL_S` (AC2).
            self._link.register_soon()
        if (
            sup.is_running()
            and self._failure is not None
            and self._failure.startswith("tunnel:")
        ):
            self._failure = None

    def _redial(self) -> None:
        """Act on the link's verdict: a duplicate identity must not hold a
        tunnel open (two twins on one subdomain split traffic between them),
        and everything else dials."""
        if not self._started:
            # Reap before accepting the relay's verdict: an orphan from the
            # previous daemon can itself be the incumbent that caused a 409.
            self._reap()
        if not self._link.should_dial():
            if self._started:
                self._supervisor.stop()
                self._started = False
                self._online = False
            return
        if not self._started:
            # Once start is attempted, every retry belongs to the supervisor's
            # capped backoff — including failures before a child is returned.
            self._started = True
            self._supervisor.start()
            # This tick's `link.tick()` already published the entry for this
            # child, so the initial dial must not also count as a respawn.
            self._spawns_seen = self._supervisor.spawn_count
            if (
                self._supervisor.is_running()
                and self._failure is not None
                and self._failure.startswith("tunnel:")
            ):
                self._failure = None

    def _reap(self) -> None:
        """Sweep orphans once per downtime, not once per tick: this shells out to
        the process table, and a tunnel that is down for an hour would otherwise
        do it hundreds of times."""
        if self._reaped:
            return
        self._reaper(self._link.subdomain)
        self._reaped = True


    def _read_back(self, now: float) -> None:
        """Ask our own public URL who is answering on it.

        Three outcomes, and the difference between the last two is the whole
        point: a DIFFERENT `instance_id` is a live twin (terminal — a human has
        to sort it out), while no answer at all is just an unreachable relay
        (transient), and conflating them would report every network blip as a
        clone. Same split `health.py` makes between a stale token and a route
        that has not come up yet.
        """
        if (
            self._last_readback_at is not None
            and 0 <= now - self._last_readback_at < self._readback_interval
        ):
            return
        self._last_readback_at = now
        probe = self._verify(self._link.public_url, "")
        # Contention is an observation, not a latch: every completed read-back
        # supersedes the previous verdict, even when the new result is transient.
        self._contended_with = None
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
            self._detail = (
                f"read-back of {self._link.public_url} carried no instance_id"
            )
            return
        if answered == self._link.instance_id:
            self._online = True
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
        far along the dial is.

        `contended` deliberately outranks `error`: a live twin answering on our
        subdomain is a specific, named, operator-actionable fact, while `error`
        is the catch-all for "a tick faulted or registration is failing" — and
        the two co-occur constantly (a twin holding the route is exactly what
        makes our own registrations fail), so the specific verdict has to win or
        it would never be the one displayed.
        """
        if self._stopped:
            return "disabled"
        if not self._link.should_dial():
            return "duplicate-identity"
        if self._link.state == "awaiting-enrollment":
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
    def host_id(self) -> str:
        return self._link.host_id

    @property
    def instance_id(self) -> str:
        return self._link.instance_id

    @property
    def subdomain(self) -> str:
        return self._link.subdomain

    @property
    def public_url(self) -> str:
        return self._link.public_url

    @property
    def restart_count(self) -> int:
        return self._supervisor.restart_count

    def snapshot(self) -> dict:
        """What `/health` (and through it `mship daemon status`) publishes.

        A PUBLICATION, not a live read: ticks run in the daemon's executor
        (`run.py::_tunnel_loop`) while requests are served on the loop thread,
        so a reader assembling this field-by-field could see half of one tick
        and half of the next. `_publish` builds a fresh dict at the end of every
        tick and nothing ever mutates a published one, which makes a read a
        single attribute load."""
        return self._published

    def _publish(self) -> None:
        self._published = {
            "state": self.state(),
            "subdomain": self._link.subdomain,
            "public_url": self._link.public_url,
            "restarts": self._supervisor.restart_count,
            "last_error": self.last_error,
            # The ENROLL SERVER's clock, sampled by the link — never the
            # read-back's, which is this host's own uvicorn (~0 by
            # construction). Reported only; it gates nothing.
            "clock_skew_seconds": self._link.clock_skew_seconds,
        }
