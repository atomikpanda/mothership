"""The daemon's tunnel + registration loop (#471 Task 7).

`HostTunnel` joins two things that already exist — `TunnelSupervisor` (the ssh
child) and `RelayLink` (the directory entry) — and adds the three facts neither
can know alone: an orphaned `ssh -R` must be reaped before we dial, a respawn
must re-register exactly once, and the only proof that *we* hold our subdomain
is reading our own `instance_id` back off it.

Seams are injected exactly like `tests/core/relay/test_tunnel_supervisor.py`
(`FakeProc` + a list clock) and `tests/core/daemon/test_relay_link.py`
(scripted `post`/`get`): no sockets, no sleeps, no ssh-keygen.
"""

import base64
import hashlib
import os
import signal
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mship.core.daemon import history
from mship.core.daemon.host_app import create_host_app
from mship.core.daemon.host_tunnel import (
    ORPHAN_EXIT_TIMEOUT_S,
    STATES,
    HostTunnel,
    ProcessInfo,
    list_processes,
    reap_orphan_tunnels,
)
from mship.core.daemon.identity import HostIdentity
from mship.core.daemon.paths import registry_path, start_history_path, tunnel_log_path
from mship.core.daemon.registry import RegistryStore
from mship.core.daemon.relay_link import RelayLink
from mship.core.relay import host_contract
from mship.core.relay.config import RelayConfig
from mship.core.relay.health import probe_health
from mship.core.relay.keys import ensure_subdomain_secret, relay_key_path
from mship.core.relay.tunnel import (
    TunnelSupervisor,
    build_tunnel_argv,
    device_id,
    host_subdomain,
)

RELAY = RelayConfig(host="relay.example")
BASE = host_contract.enroll_base_url(RELAY.host)
PUBKEY = "ssh-ed25519 " + base64.b64encode(b"k" * 51).decode() + " mship-relay\n"
BIND_PORT = 8765
UNAPPROVED = host_contract.UNAPPROVED_KEY_DETAIL


# --- the seams -------------------------------------------------------------


class _Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeProc:
    def __init__(self):
        self._alive = True
        self.terminated = 0

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated += 1
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = {} if body is None else body
        self.headers = {}

    def json(self):
        return self._body


class _Relay:
    """Scriptable stand-in for the enroll app's `/hosts/*` + `/enroll` routes."""

    def __init__(self, register=(200, None)):
        self.register_status, self.register_detail = register
        self.registrations: list[dict] = []
        self.enrollments: list[dict] = []
        self.transport_error: str | None = None

    def post(self, url, json=None, **kw):
        if self.transport_error:
            raise RuntimeError(self.transport_error)
        if url.endswith(host_contract.CHALLENGE_PATH):
            return _Resp(200, {"nonce": "nonce-1"})
        if url.endswith(host_contract.ENROLL_PATH):
            self.enrollments.append(json)
            return _Resp(200, {"id": "req-1", "status": "pending"})
        self.registrations.append(json["payload"])
        if self.register_status == 200:
            return _Resp(200, {"status": "registered"})
        return _Resp(self.register_status, {"detail": self.register_detail})

    def refuse(self, status: int, detail: str | None = None) -> None:
        self.register_status, self.register_detail = status, detail


def _seed_key(home: Path) -> None:
    key = relay_key_path(home)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("PRIVATE")
    key.with_name(key.name + ".pub").write_text(PUBKEY)


def _link(home: Path, relay: _Relay, clock: _Clock) -> RelayLink:
    _seed_key(home)
    return RelayLink(
        home,
        RELAY,
        post=relay.post,
        clock=clock,
        rng=lambda: 0.0,
        signer=lambda blob: "SIG:" + hashlib.sha256(blob).hexdigest(),
        issue_refresh=lambda host_id: f"refresh-for-{host_id}",
        reidentify=lambda: HostIdentity(host_id="hst-new", created_at=""),
    )


def _health_get(
    instance_id: str | None, *, error: str | None = None, status: int = 200
):
    def get(url, **kw):
        if error is not None:
            raise RuntimeError(error)
        body = (
            {"status": "ok"}
            if instance_id is None
            else {"status": "ok", "instance_id": instance_id}
        )
        return _Resp(status, body)

    return get


class _Fixture:
    """One wired-up tunnel: link + supervisor + the argv a daemon would build."""

    def __init__(
        self,
        home: Path,
        relay: _Relay,
        clock: _Clock,
        *,
        backoff: float = 0.0,
        max_backoff: float = 60.0,
        **kw,
    ):
        self.home, self.relay, self.clock = home, relay, clock
        self.procs: list[FakeProc] = []
        self.argvs: list[list[str]] = []
        self.reaped: list[str] = []
        self.link = _link(home, relay, clock)
        self.argv = self._argv()
        self.sup = TunnelSupervisor(
            argv=self._argv,
            proc_factory=self._factory,
            backoff_delay=backoff,
            max_backoff_delay=max_backoff,
            clock=clock,
            log_path=tunnel_log_path(home),
            rng=lambda: 0.0,
        )
        kw.setdefault(
            "verify", partial(probe_health, get=_health_get(self.link.instance_id))
        )
        kw.setdefault("reaper", self._reaper)
        self.tunnel = HostTunnel(self.link, self.sup, clock=clock, **kw)

    def _argv(self):
        return build_tunnel_argv(
            RELAY,
            subdomain=self.link.subdomain,
            local_port=BIND_PORT,
            key_path=relay_key_path(self.home),
        )

    def _factory(self, argv):
        self.argvs.append(list(argv))
        p = FakeProc()
        self.procs.append(p)
        return p

    def _reaper(self, subdomain):
        self.reaped.append(subdomain)
        return []

    @property
    def child(self) -> FakeProc:
        return self.procs[-1]

    def kill_child(self) -> None:
        """The ssh child exits on its own (a dropped relay), not via stop()."""
        self.procs[-1]._alive = False

    def connect(self) -> str:
        """Two ticks: the first dials (only after the link has had its say), the
        second reads back off the now-live tunnel."""
        self.tunnel.tick()
        self.clock.advance(1)
        return self.tunnel.tick()

    def drop_and_wait(self, step: float = 0.5, limit: int = 40) -> None:
        """Kill the child, then tick until the supervisor's backoff lets a
        replacement through — the real shape, rather than a zero backoff."""
        before = len(self.procs)
        self.kill_child()
        for _ in range(limit):
            self.tunnel.tick()
            if len(self.procs) > before:
                return
            self.clock.advance(step)
        raise AssertionError("supervisor never respawned within the tick budget")

    def write_ssh_log(self, text: str) -> None:
        path = tunnel_log_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def fx(tmp_path: Path):
    return _Fixture(tmp_path, _Relay(), _Clock())


# --- dialing ----------------------------------------------------------------


def test_tick_dials_the_host_subdomain_on_the_daemons_bind_port(fx):
    fx.tunnel.tick()

    assert len(fx.procs) == 1
    assert fx.argvs[0] == fx.argv
    assert "-R" in fx.argvs[0]
    assert f"{fx.link.subdomain}:80:localhost:{BIND_PORT}" in fx.argvs[0]
    # the label is the HOST subdomain — derived from the host id, never from a
    # workspace name (assumption 1: nothing here takes a workspace as input)
    label = fx.argvs[0][fx.argvs[0].index("-R") + 1].split(":")[0]
    assert label == host_subdomain(
        fx.link.host_id, device_id(PUBKEY), ensure_subdomain_secret(fx.home)
    )


def test_initial_spawn_failure_uses_the_supervisor_backoff(tmp_path):
    clock = _Clock()
    link = _link(tmp_path, _Relay(), clock)
    attempts = []

    def fail_to_spawn(_argv):
        attempts.append(clock())
        raise FileNotFoundError("ssh")

    supervisor = TunnelSupervisor(
        argv=["ssh"],
        proc_factory=fail_to_spawn,
        backoff_delay=5,
        max_backoff_delay=60,
        clock=clock,
        rng=lambda: 0.0,
        log_path=tmp_path,
    )
    tunnel = HostTunnel(
        link,
        supervisor,
        clock=clock,
        reaper=lambda _subdomain: [],
    )

    assert tunnel.tick() == "error"
    for _ in range(4):
        clock.advance(1)
        tunnel.tick()
        assert tunnel.state() == "error"
        assert "ssh" in tunnel.last_error
    assert attempts == [1_000_000.0]

    clock.advance(1)
    tunnel.tick()
    assert attempts == [1_000_000.0, 1_000_005.0]


def test_tick_registers_and_reads_back_to_go_online(fx):
    assert fx.connect() == "online"

    assert len(fx.relay.registrations) == 1
    assert fx.tunnel.last_error is None


def test_no_ssh_process_is_ever_spawned_while_the_link_says_not_to_dial(tmp_path):
    """AC4: a 409 means a live twin holds the subdomain; dialing would fight it
    for the route and split traffic between the two of us."""
    relay = _Relay(register=(409, "identity already registered"))
    fx = _Fixture(tmp_path, relay, _Clock())

    for _ in range(RelayLink.DUPLICATE_REIDENTIFY_AFTER - 1):
        fx.tunnel.tick()
        fx.clock.advance(120)

    assert fx.procs == [], "dialed while the relay says another host holds our identity"
    assert fx.tunnel.state() == "duplicate-identity"

    # ...and the recovery path un-blocks the dial on the newly minted identity:
    # the old key/subdomain cannot serve the new directory entry.
    old_subdomain = fx.link.subdomain
    fx.tunnel.tick()
    assert fx.tunnel.state() == "awaiting-enrollment"
    assert len(fx.procs) == 1
    assert old_subdomain not in " ".join(fx.argvs[-1])
    assert fx.link.subdomain in " ".join(fx.argvs[-1])


def test_duplicate_identity_still_reaps_a_local_orphan_before_refusing_to_dial(
    tmp_path,
):
    fx = _Fixture(
        tmp_path,
        _Relay(register=(409, "identity already registered")),
        _Clock(),
    )

    fx.tunnel.tick()

    assert fx.reaped == [fx.link.subdomain]
    assert fx.procs == []


def test_a_tunnel_already_up_is_torn_down_when_the_identity_goes_duplicate(fx):
    assert fx.connect() == "online"

    fx.relay.refuse(409, "identity already registered")
    fx.clock.advance(120)
    fx.tunnel.tick()

    assert fx.tunnel.state() == "duplicate-identity"
    assert fx.child.terminated == 1


# --- AC2: respawn re-registers exactly once ---------------------------------


def test_a_respawn_triggers_exactly_one_additional_registration(fx):
    fx.connect()
    assert len(fx.relay.registrations) == 1

    fx.kill_child()
    fx.clock.advance(1)
    fx.tunnel.tick()  # respawn + the one extra registration

    assert len(fx.procs) == 2
    assert len(fx.relay.registrations) == 2

    for _ in range(20):  # a re-register storm is the wrong shape
        fx.clock.advance(1)
        fx.tunnel.tick()
    assert len(fx.relay.registrations) == 2, (
        "re-registered once per tick, not once per respawn"
    )


def test_a_drop_after_a_healthy_run_registers_once_and_never_while_the_child_is_gone(
    tmp_path,
):
    """The regression the zero-backoff fixture cannot see: `restart_count` is
    reset by a healthy run, so diffing it reads the DROP as a respawn and
    re-registers while no child exists, then registers again on the respawn."""
    fx = _Fixture(tmp_path, _Relay(), _Clock(), backoff=1.0, max_backoff=8.0)
    started = fx.clock.t
    fx.connect()

    for _ in range(3):  # a flap streak builds the count up
        fx.drop_and_wait()
    fx.clock.advance(9.0)  # ...then a run outliving the cap
    fx.tunnel.tick()
    assert fx.sup.is_running() and fx.sup.restart_count == 3

    before = len(fx.relay.registrations)
    fx.kill_child()
    fx.tunnel.tick()  # the drop is detected; nothing is up
    assert fx.sup.restart_count == 0, "the healthy run should have reset the streak"
    assert len(fx.relay.registrations) == before, "registered while no child existed"

    fx.clock.advance(1.0)
    fx.tunnel.tick()
    assert len(fx.procs) == 5
    assert len(fx.relay.registrations) == before + 1

    for _ in range(5):
        fx.clock.advance(0.5)
        fx.tunnel.tick()
    assert len(fx.relay.registrations) == before + 1
    assert fx.clock.t - started < host_contract.REGISTER_INTERVAL_S, (
        "the link's own schedule came due — the counts above stopped meaning anything"
    )


def test_redialing_after_a_duplicate_identity_clears_does_not_register_twice(tmp_path):
    """`TunnelSupervisor.start()` zeroes `restart_count`, so the teardown +
    re-dial cycle looks like a respawn to anything that diffs it.

    The flap streak is load-bearing: with a counter sitting at 0 on both sides of
    the teardown there is no difference to observe, and this test would pass
    against the very implementation it exists to rule out."""
    fx = _Fixture(tmp_path, _Relay(), _Clock(), backoff=1.0, max_backoff=8.0)
    fx.connect()
    for _ in range(3):
        fx.drop_and_wait()
    assert fx.sup.restart_count == 3

    fx.relay.refuse(409, "identity already registered")
    fx.clock.advance(120)
    fx.tunnel.tick()
    assert fx.tunnel.state() == "duplicate-identity" and not fx.sup.is_running()

    fx.relay.refuse(200)
    fx.clock.advance(120)
    fx.tunnel.tick()  # re-dials, and registers once, here
    assert len(fx.procs) == 5 and fx.sup.restart_count == 0
    after_redial = len(fx.relay.registrations)

    for _ in range(5):
        fx.clock.advance(1)
        fx.tunnel.tick()
    assert len(fx.relay.registrations) == after_redial


def test_registration_failure_does_not_kill_the_tunnel(fx):
    fx.connect()
    fx.relay.transport_error = "connection refused"

    for _ in range(30):
        fx.clock.advance(120)
        fx.tunnel.tick()

    assert fx.sup.is_running(), "a registration outage tore down a healthy tunnel"
    assert fx.child.terminated == 0


def test_tunnel_failure_does_not_stop_registration_retries(fx):
    fx.connect()
    before = len(fx.relay.registrations)
    fx.kill_child()

    fx.procs.clear()  # the ssh child now refuses to come back
    fx.tunnel._supervisor._proc_factory = lambda argv: (_ for _ in ()).throw(
        OSError("ssh: command not found")
    )

    for _ in range(5):
        fx.clock.advance(120)
        fx.tunnel.tick()

    assert len(fx.relay.registrations) > before, "a dead tunnel silenced registration"


# --- AC7: the tunnel shares no lifetime with the registry or the host app ---


def test_fifty_failing_ticks_leave_the_registry_and_host_app_untouched(fx):
    store = RegistryStore(registry_path(fx.home))
    store.mutate(lambda s: s)  # materialise the file
    app = create_host_app(
        store, auth_token="tok", host_id="hst-1", instance_id="inst-1"
    )
    before = registry_path(fx.home).read_bytes()

    fx.relay.transport_error = "network is unreachable"
    fx.tunnel._supervisor._proc_factory = lambda argv: (_ for _ in ()).throw(
        OSError("ssh: connect failed")
    )
    for _ in range(50):
        fx.clock.advance(120)
        fx.tunnel.tick()

    assert registry_path(fx.home).read_bytes() == before
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_an_exception_from_a_tick_lands_in_last_error_and_the_loop_keeps_running(fx):
    attempts = []

    def boom(subdomain):
        attempts.append(subdomain)
        raise RuntimeError("ps: no such process table")

    fx.tunnel._reaper = boom

    assert fx.tunnel.tick() == "error"
    assert fx.tunnel.tick() == "error"
    assert attempts == [fx.link.subdomain, fx.link.subdomain]
    assert fx.procs == []
    assert "no such process table" in fx.tunnel.last_error

    fx.tunnel._reaper = fx._reaper  # the fault clears
    fx.clock.advance(120)
    assert fx.connect() == "online"
    assert fx.tunnel.last_error is None


def test_stop_terminates_the_child_exactly_once_and_is_idempotent(fx):
    fx.connect()
    child = fx.child

    fx.tunnel.stop()
    fx.tunnel.stop()
    fx.tunnel.stop()

    assert child.terminated == 1
    assert fx.tunnel.state() == "disabled"

    fx.clock.advance(120)
    fx.tunnel.tick()
    assert len(fx.procs) == 1, "ticking after stop() re-dialed the relay"


# --- AC11: reconnecting writes nothing --------------------------------------


def test_twenty_failure_respawn_cycles_leave_the_registry_and_journal_byte_identical(
    fx,
):
    store = RegistryStore(registry_path(fx.home))
    store.mutate(lambda s: s)
    journal = start_history_path(fx.home)
    history.append_start(journal, datetime(2026, 8, 18, tzinfo=timezone.utc))
    before = (registry_path(fx.home).read_bytes(), journal.read_bytes())

    fx.connect()
    for _ in range(20):
        fx.kill_child()
        fx.clock.advance(1)
        fx.tunnel.tick()

    assert len(fx.procs) == 21
    assert (registry_path(fx.home).read_bytes(), journal.read_bytes()) == before
    # ...and one directory entry, re-published rather than re-minted
    assert {r["host_id"] for r in fx.relay.registrations} == {fx.link.host_id}
    assert {r["refresh"] for r in fx.relay.registrations} == {
        f"refresh-for-{fx.link.host_id}"
    }


# --- the orphan reaper ------------------------------------------------------


def _proc(pid, ppid, cmdline, started=None):
    return ProcessInfo(
        pid=pid, ppid=ppid, cmdline=cmdline, started=started or f"start-{pid}"
    )


def test_reaper_kills_an_orphaned_tunnel_on_our_subdomain(fx):
    """The collision class `scripts/redeploy-serve.sh` sweeps by hand: a `kill -9`
    leaves the `start_new_session` ssh child reparented to init, still holding
    the subdomain, so sish rejects the fresh tunnel and the relay 404s."""
    ours = _proc(
        200, 42, f"ssh -N -R {fx.link.subdomain}:80:localhost:8765 relay.example"
    )
    orphan = _proc(
        100, 1, f"ssh -N -R {fx.link.subdomain}:80:localhost:8765 relay.example"
    )
    killed = []
    alive = {orphan.pid}

    def terminate(pid):
        killed.append(pid)
        alive.remove(pid)

    reaped = reap_orphan_tunnels(
        fx.link.subdomain,
        processes=lambda: [ours, orphan],
        kill=terminate,
        process_exists=lambda pid: pid in alive,
    )

    assert reaped == [100] and killed == [100]


def test_reaper_waits_then_force_kills_a_stubborn_orphan(fx):
    orphan = _proc(
        100, 1, f"ssh -N -R {fx.link.subdomain}:80:localhost:8765 relay.example"
    )
    now = [0.0]
    alive = {orphan.pid}
    signals = []

    def force_kill(pid):
        signals.append((pid, signal.SIGKILL))
        alive.remove(pid)

    reap_orphan_tunnels(
        fx.link.subdomain,
        processes=lambda: [orphan],
        kill=lambda pid: signals.append((pid, signal.SIGTERM)),
        force_kill=force_kill,
        process_exists=lambda pid: pid in alive,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert signals == [(orphan.pid, signal.SIGTERM), (orphan.pid, signal.SIGKILL)]
    assert now[0] >= ORPHAN_EXIT_TIMEOUT_S


def test_reaper_does_not_force_kill_a_reused_pid(fx):
    orphan = _proc(
        100, 1, f"ssh -N -R {fx.link.subdomain}:80:localhost:8765 relay.example"
    )
    replacement = _proc(
        orphan.pid,
        1,
        f"ssh -N -R {fx.link.subdomain}:80:localhost:8765 relay.example",
        started="replacement",
    )
    rows = [orphan]
    now = [0.0]
    signals = []

    def terminate(pid):
        signals.append((pid, signal.SIGTERM))
        rows[0] = replacement

    reap_orphan_tunnels(
        fx.link.subdomain,
        processes=lambda: list(rows),
        kill=terminate,
        force_kill=lambda pid: pytest.fail(f"force-killed reused pid {pid}"),
        process_exists=lambda _pid: True,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert signals == [(orphan.pid, signal.SIGTERM)]


def test_reaper_does_not_term_a_pid_reused_before_the_first_signal(fx):
    orphan = _proc(
        100, 1, f"ssh -N -R {fx.link.subdomain}:80:localhost:8765 relay.example"
    )
    replacement = _proc(
        orphan.pid,
        1,
        f"ssh -N -R {fx.link.subdomain}:80:localhost:8765 relay.example",
        started="replacement",
    )
    snapshots = iter(([orphan], [replacement]))
    opened = []
    closed = []
    signals = []

    reaped = reap_orphan_tunnels(
        fx.link.subdomain,
        processes=lambda: next(snapshots),
        pidfd_open=lambda pid: (opened.append(pid), 77)[1],
        pidfd_signal=lambda fd, sig: signals.append((fd, sig)),
        close_pidfd=closed.append,
        process_exists=lambda _pid: True,
    )

    assert reaped == []
    assert opened == [orphan.pid]
    assert closed == [77]
    assert signals == []


def test_reaper_refuses_to_signal_without_atomic_pidfd_support(fx):
    orphan = _proc(
        100, 1, f"ssh -N -R {fx.link.subdomain}:80:localhost:8765 relay.example"
    )

    with pytest.raises(RuntimeError, match="atomic pidfd signaling"):
        reap_orphan_tunnels(
            fx.link.subdomain,
            processes=lambda: [orphan],
            pidfd_open=lambda _pid: (_ for _ in ()).throw(
                NotImplementedError("pidfd unavailable")
            ),
        )


def test_reaper_leaves_other_hosts_tunnels_and_non_tunnels_alone(fx):
    other = _proc(101, 1, "ssh -N -R someone-else:80:localhost:8765 relay.example")
    shell = _proc(102, 1, f"ssh relay.example  # mentions {fx.link.subdomain}")
    killed = []

    reaped = reap_orphan_tunnels(
        fx.link.subdomain,
        processes=lambda: [other, shell],
        kill=lambda pid: killed.append(pid),
    )

    assert reaped == [] and killed == []


def test_reaper_spares_a_label_that_merely_ends_with_ours(fx):
    """A substring match would take down an unrelated host: its own opaque slug
    can end with the whole of ours, and it is orphaned-looking to us either way.
    The label is a token, so it is compared as one."""
    collider = _proc(
        103, 1, f"ssh -N -R xyz-{fx.link.subdomain}:80:localhost:9000 relay.example"
    )
    prefix = _proc(
        104, 1, f"ssh -N -R {fx.link.subdomain}-extra:80:localhost:9000 relay.example"
    )
    killed = []

    reaped = reap_orphan_tunnels(
        fx.link.subdomain,
        processes=lambda: [collider, prefix],
        kill=lambda pid: killed.append(pid),
    )

    assert reaped == [] and killed == []


def test_reaper_tolerates_a_trailing_dash_r_with_no_forward_spec(fx):
    dangling = _proc(105, 1, "ssh -N relay.example -R")
    assert (
        reap_orphan_tunnels(
            fx.link.subdomain,
            processes=lambda: [dangling],
            kill=lambda pid: pytest.fail("signalled a malformed row"),
        )
        == []
    )


def test_reaper_survives_a_process_that_vanished_between_list_and_kill(fx):
    def kill(pid):
        raise ProcessLookupError(pid)

    orphan = _proc(
        100, 1, f"ssh -N -R {fx.link.subdomain}:80:localhost:8765 relay.example"
    )

    assert (
        reap_orphan_tunnels(fx.link.subdomain, processes=lambda: [orphan], kill=kill)
        == []
    )


def test_the_default_lister_parses_the_real_process_table():
    """The sweep is silently a no-op if the `ps` parsing is wrong, so pin it
    against the real table rather than only against hand-built rows."""
    rows = list_processes()

    assert rows, "could not read the process table at all"
    me = next(r for r in rows if r.pid == os.getpid())
    assert me.ppid == os.getppid()
    assert me.cmdline, "the command line is what the subdomain is matched in"


def test_orphans_are_reaped_before_dialing_and_before_each_respawn(fx):
    fx.tunnel.tick()
    assert fx.reaped == [fx.link.subdomain], "dialed without sweeping orphans first"

    for _ in range(5):  # not once per tick while the child holds
        fx.clock.advance(1)
        fx.tunnel.tick()
    assert len(fx.reaped) == 1

    fx.kill_child()
    fx.clock.advance(1)
    fx.tunnel.tick()
    assert len(fx.reaped) == 2 and len(fx.procs) == 2


# --- the state ladder -------------------------------------------------------


def test_awaiting_enrollment_is_classified_from_the_ssh_log_tail(fx):
    """ssh's own refusal is the first evidence that the relay has the key but
    nobody has approved it — it arrives before any registration verdict."""
    fx.write_ssh_log("Permission denied (publickey).\n")
    fx.relay.transport_error = "connection refused"

    fx.tunnel.tick()
    fx.kill_child()
    fx.clock.advance(120)
    fx.tunnel.tick()

    assert fx.tunnel.state() == "awaiting-enrollment"


def test_awaiting_enrollment_is_classified_from_the_relays_verdict(tmp_path):
    relay = _Relay(register=(401, UNAPPROVED))
    fx = _Fixture(tmp_path, relay, _Clock())

    fx.tunnel.tick()

    assert fx.tunnel.state() == "awaiting-enrollment"
    assert fx.relay.enrollments, "did not (re-)post the key for approval"


def test_read_back_of_our_own_instance_id_is_online(fx):
    assert fx.connect() == "online"


def test_a_foreign_instance_id_is_contended_and_does_not_tear_the_tunnel_down(tmp_path):
    """Comparing host_id would be blind to exactly this case: a `cp -a` twin
    publishes the SAME host_id on the SAME subdomain. Only the per-process
    instance_id tells the two apart."""
    fx = _Fixture(
        tmp_path,
        _Relay(),
        _Clock(),
        verify=partial(probe_health, get=_health_get("inst-of-the-twin")),
    )

    state = fx.connect()

    assert state == "contended"
    assert "inst-of-the-twin" in fx.tunnel.last_error
    assert fx.sup.is_running() and fx.child.terminated == 0


@pytest.mark.parametrize(
    "next_get",
    (
        _health_get(None, error="connection refused"),
        _health_get(None, status=503),
        _health_get(None),
    ),
    ids=("transport-error", "non-2xx", "missing-instance-id"),
)
def test_a_transient_readback_replaces_an_obsolete_contention_verdict(
    tmp_path, next_get
):
    responses = iter((_health_get("inst-of-the-twin"), next_get))

    def get(*args, **kwargs):
        return next(responses)(*args, **kwargs)

    fx = _Fixture(
        tmp_path,
        _Relay(),
        _Clock(),
        verify=partial(probe_health, get=get),
    )
    assert fx.connect() == "contended"

    fx.clock.advance(120)

    assert fx.tunnel.tick() == "connecting"


def test_a_transport_failure_on_the_read_back_stays_connecting(tmp_path):
    """Unreachable is not contended — the same terminal-vs-transient split
    `health.py` makes for a stale token vs a route that has not come up yet."""
    fx = _Fixture(
        tmp_path,
        _Relay(),
        _Clock(),
        verify=partial(probe_health, get=_health_get(None, error="connection refused")),
    )

    for _ in range(5):
        fx.clock.advance(120)
        assert fx.tunnel.tick() == "connecting"
    assert "connection refused" in fx.tunnel.last_error


def test_a_read_back_without_an_instance_id_stays_connecting(tmp_path):
    fx = _Fixture(
        tmp_path,
        _Relay(),
        _Clock(),
        verify=partial(probe_health, get=_health_get(None)),
    )
    assert fx.connect() == "connecting"


def test_a_registration_that_keeps_failing_is_error_not_online(fx):
    fx.connect()
    fx.relay.refuse(500, "boom")

    fx.clock.advance(120)
    assert fx.tunnel.tick() == "error"
    assert fx.sup.is_running(), "a directory failure tore down a working tunnel"


def test_state_covers_the_whole_vocabulary(tmp_path):
    """Every state `mship daemon status` renders must be reachable from here."""
    seen = set()

    fx = _Fixture(tmp_path / "a", _Relay(), _Clock())
    seen.add(fx.tunnel.state())  # connecting (nothing ticked yet)
    seen.add(fx.connect())  # online
    fx.tunnel.stop()
    seen.add(fx.tunnel.state())  # disabled

    contended = _Fixture(
        tmp_path / "b",
        _Relay(),
        _Clock(),
        verify=partial(probe_health, get=_health_get("inst-twin")),
    )
    seen.add(contended.connect())

    unapproved = _Fixture(tmp_path / "c", _Relay(register=(401, UNAPPROVED)), _Clock())
    seen.add(unapproved.connect())

    dup = _Fixture(
        tmp_path / "d", _Relay(register=(409, "already registered")), _Clock()
    )
    seen.add(dup.connect())

    broken = _Fixture(tmp_path / "e", _Relay(), _Clock())
    broken.tunnel._reaper = lambda subdomain: 1 / 0
    seen.add(broken.tunnel.tick())

    assert seen == set(STATES)


def test_stop_landing_inside_a_tick_spawns_nothing_afterwards(tmp_path):
    """AC7's orphan window: `stop()` runs on the daemon's loop thread while a
    tick runs in the executor. A tick already past its own stopped-check goes on
    to `_redial`, and without a latch that call would fork a fresh
    `start_new_session=True` ssh AFTER the child was signalled — an orphan
    holding the subdomain that nothing in this process owns any more."""
    clock = _Clock()
    fixture = _Fixture(tmp_path, _Relay(), clock)
    real_link_tick = fixture.link.tick

    def shutdown_lands_mid_tick():
        fixture.tunnel.stop()
        return real_link_tick()

    fixture.link.tick = shutdown_lands_mid_tick

    fixture.tunnel.tick()

    assert fixture.procs == [], "dialed a tunnel after the daemon stopped it"
    assert fixture.tunnel.state() == "disabled"


def test_a_child_respawned_in_the_same_tick_as_the_stop_is_still_terminated(
    tmp_path,
):
    """The same window one layer in: the respawn happens earlier in the very
    tick the shutdown lands in. That child must be signalled and must be the
    last one — the danger is not the spawn, it is a spawn nothing then owns."""
    clock = _Clock()
    fixture = _Fixture(tmp_path, _Relay(), clock)
    fixture.connect()
    fixture.kill_child()
    clock.advance(120)  # any backoff has long since elapsed
    real_link_tick = fixture.link.tick

    def shutdown_lands_mid_tick():
        fixture.tunnel.stop()
        return real_link_tick()

    fixture.link.tick = shutdown_lands_mid_tick

    fixture.tunnel.tick()
    spawned_by_shutdown_tick = len(fixture.procs)
    fixture.tunnel.tick()
    clock.advance(120)
    fixture.tunnel.tick()

    assert fixture.child.terminated, "the respawned child outlived the stop"
    assert len(fixture.procs) == spawned_by_shutdown_tick


# --- the snapshot the daemon publishes (#471 Task 9) ------------------------


def test_snapshot_is_published_per_tick_not_read_live(fx):
    """`/health` serves this from the request thread while ticks mutate the
    tunnel on the executor thread (`run.py::_tunnel_loop`), so a snapshot must
    be an immutable per-tick publication: the dict a reader already holds can
    never change under it, and a later tick publishes a NEW one."""
    before = fx.tunnel.snapshot()
    assert before["state"] == "connecting"

    fx.connect()
    after = fx.tunnel.snapshot()

    assert before["state"] == "connecting", "a published snapshot mutated"
    assert after is not before
    assert after["state"] == "online"
    assert after["subdomain"] == fx.link.subdomain
    assert after["public_url"] == fx.link.public_url
    assert after["restarts"] == 0
    assert after["last_error"] is None
    assert after["clock_skew_seconds"] is None  # the fake relay sends no Date


def test_snapshot_reports_the_states_and_reasons_the_operator_acts_on(tmp_path):
    dup = _Fixture(
        tmp_path / "dup", _Relay(register=(409, "held by hst-other")), _Clock()
    )
    dup.connect()
    assert dup.tunnel.snapshot()["state"] == "duplicate-identity"
    assert "held by hst-other" in dup.tunnel.snapshot()["last_error"]

    fx = _Fixture(tmp_path / "up", _Relay(), _Clock())
    fx.connect()
    fx.tunnel.stop()
    assert fx.tunnel.snapshot()["state"] == "disabled"


def test_snapshot_carries_the_links_clock_skew(fx):
    """Sourced from the ENROLL SERVER's `Date` (Task 6), never the read-back's:
    that one is emitted by this host's own uvicorn and is ~0 by construction."""
    fx.link.clock_skew_seconds = -3600.0
    fx.tunnel.tick()
    assert fx.tunnel.snapshot()["clock_skew_seconds"] == -3600.0
