"""mshipd run loop: lease → history → logs → uvicorn over the unix socket.

Loser exit-code policy (cross-OS, decided up front): confirmed-live holder →
exit 0 (launchd's KeepAlive.SuccessfulExit=false relaunches on ANY nonzero exit
every ThrottleInterval — a permanent hot loop; systemd Restart=on-failure
already skips 0). Contended-but-dead → nonzero so the supervisor retries.
"""
import json
import logging
import multiprocessing
import tomllib
from pathlib import Path

import pytest

import mship.core.daemon.host_tunnel as host_tunnel_mod
import mship.core.daemon.relay_link as relay_link_mod
import mship.core.daemon.run as run_mod
from mship.core.daemon.history import read_history
from mship.core.daemon.lease import DaemonLease
from mship.core.daemon.paths import daemon_log_dir, daemon_socket_path, lease_path

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def env_home(tmp_path, monkeypatch):
    """Isolated home + env for main(); returns (home, env)."""
    home = tmp_path / "home"
    home.mkdir()
    env = {}
    # Reset logging state main() configures so tests stay independent.
    yield home, env
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()


def _fake_uvicorn(monkeypatch, *, on_serve=None, hold=False):
    """Stub uvicorn's Config/Server, and forbid `uvicorn.run` outright.

    The capture seam moved with the code: `_serve_forever` runs on asyncio in
    every shape now (#471 — a tunnel loop has to live beside the servers, and
    `uvicorn.run` owns the loop), so what a startup binds is visible on the
    Config it builds rather than on a `run()` call. Reaching `uvicorn.run` at
    all is now itself a failure, which is what pins the asyncio branch.

    Returns `(configs, servers)`, in construction order and unfiltered — the
    shape of a startup is how many servers it builds and what each was given,
    so a test asserts both rather than reading a dict that only ever holds the
    kwargs it expected. `hold=True` makes `serve()` run until something sets
    `should_exit`, so a test can drive shutdown; otherwise each server serves
    once and reports the clean exit a signalled uvicorn does.
    """
    import asyncio

    import uvicorn

    configs: list = []
    servers: list = []

    class _Config:
        def __init__(self, app, **kwargs):
            self.app = app
            self.kwargs = kwargs
            self.host = kwargs.get("host")
            configs.append(self)

    class _Server:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False
            servers.append(self)

        async def serve(self):
            self.started = True
            if on_serve is not None:
                on_serve()
            while hold and not self.should_exit:
                await asyncio.sleep(0)
            # A real server returns from serve() with should_exit set once it
            # has been asked to stop; leaving it False reads as a crash.
            self.should_exit = True

    def _forbidden(app, **kwargs):
        raise AssertionError("uvicorn.run bypasses the asyncio branch (#471 AC7)")

    monkeypatch.setattr(uvicorn, "Config", _Config)
    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setattr(uvicorn, "run", _forbidden)
    return configs, servers


def _capture_uvicorn(monkeypatch, on_serve=None):
    configs, _servers = _fake_uvicorn(monkeypatch, on_serve=on_serve)
    return configs


def test_main_acquires_lease_then_serves_socket(env_home, monkeypatch):
    home, env = env_home
    lease_seen_held_by_uvicorn = {}

    def record():
        # By the time uvicorn is entered, lease + history must already exist.
        lease_seen_held_by_uvicorn["lease"] = json.loads(lease_path(home).read_text())
        lease_seen_held_by_uvicorn["history"] = [e.kind for e in read_history(run_mod.paths.start_history_path(home))]

    configs = _capture_uvicorn(monkeypatch, on_serve=record)
    rc = run_mod.main(home=home, env=env)
    assert rc == 0
    # No serve bind configured: ONE server, on the unix socket and nothing else.
    assert len(configs) == 1
    bound = configs[0].kwargs
    assert bound["uds"] == str(daemon_socket_path(env, home))
    assert "host" not in bound and "port" not in bound
    assert bound["log_config"] is None
    assert lease_seen_held_by_uvicorn["lease"]["pid"] > 0
    assert "start" in lease_seen_held_by_uvicorn["history"]


def _hold_lease(path_str: str, sock_str: str, ready: multiprocessing.Event, done: multiprocessing.Event):
    lease = DaemonLease(Path(path_str))
    assert lease.try_acquire(version="held", socket_path=sock_str) is None
    ready.set()
    done.wait(timeout=60)


@pytest.fixture
def foreign_holder(env_home):
    """A child process holding the lease; yields the socket path it recorded."""
    home, env = env_home
    sock = str(home / "held.sock")
    ctx = multiprocessing.get_context("spawn")
    ready, done = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_lease, args=(str(lease_path(home)), sock, ready, done))
    p.start()
    assert ready.wait(timeout=30)
    yield sock
    done.set()
    p.join(timeout=30)


def test_lost_race_with_live_holder_exits_zero(env_home, foreign_holder, monkeypatch, caplog):
    home, env = env_home
    called = _capture_uvicorn(monkeypatch)
    monkeypatch.setattr(run_mod, "_probe", lambda sock: {"status": "ok"} if str(sock) == foreign_holder else None)
    with caplog.at_level(logging.INFO):
        rc = run_mod.main(home=home, env=env)
    assert rc == 0
    assert called == []  # no server was ever configured
    assert any("already running" in r.message for r in caplog.records)


def test_lost_race_with_dead_holder_exits_nonzero(env_home, foreign_holder, monkeypatch):
    home, env = env_home
    called = _capture_uvicorn(monkeypatch)
    monkeypatch.setattr(run_mod, "_probe", lambda sock: None)  # holder never answers
    rc = run_mod.main(home=home, env=env)
    assert rc != 0
    assert called == []


def test_stale_socket_file_is_removed_before_bind(env_home, monkeypatch):
    home, env = env_home
    _capture_uvicorn(monkeypatch)
    sock = daemon_socket_path(env, home, create=True)
    sock.touch()  # leftover from kill -9
    assert run_mod.main(home=home, env=env) == 0
    assert not sock.exists()


def test_rotating_log_handler_configured(env_home, monkeypatch):
    home, env = env_home
    _capture_uvicorn(monkeypatch)
    assert run_mod.main(home=home, env=env) == 0
    log_file = daemon_log_dir(home) / "daemon.log"
    assert log_file.exists()
    handlers = logging.getLogger().handlers
    rotating = [h for h in handlers if h.__class__.__name__ == "RotatingFileHandler"]
    assert rotating, "root logger must carry the rotating handler"
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        assert any(h in rotating for h in lg.handlers), f"{name} not routed to daemon.log"


def test_uncaught_exception_lands_in_daemon_log(env_home, monkeypatch):
    home, env = env_home

    def boom():
        raise RuntimeError("boom")

    _capture_uvicorn(monkeypatch, on_serve=boom)
    rc = run_mod.main(home=home, env=env)
    assert rc != 0
    assert "boom" in (daemon_log_dir(home) / "daemon.log").read_text()


def test_broken_import_still_appends_history(env_home, monkeypatch):
    """The botched-upgrade class: new on-disk code fails to import. History and
    the log must still record the attempt before the nonzero exit."""
    home, env = env_home

    def broken_import():
        raise ImportError("missing dep after upgrade")

    monkeypatch.setattr(run_mod, "_import_server_stack", broken_import)
    rc = run_mod.main(home=home, env=env)
    assert rc != 0
    assert [e.kind for e in read_history(run_mod.paths.start_history_path(home))] == ["start"]
    assert "missing dep after upgrade" in (daemon_log_dir(home) / "daemon.log").read_text()


def test_clean_stop_recorded(env_home, monkeypatch):
    home, env = env_home
    _capture_uvicorn(monkeypatch)
    assert run_mod.main(home=home, env=env) == 0
    kinds = [e.kind for e in read_history(run_mod.paths.start_history_path(home))]
    assert kinds == ["start", "clean_stop"]


def test_entrypoint_registered():
    """pyproject-parsed, not entry_points() metadata — installed-dist metadata
    can be stale vs pythonpath=["src"] (the tests/test_version.py precedent)."""
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)
    assert pyproject["project"]["scripts"]["mshipd"] == "mship.core.daemon.run:main"
    assert callable(run_mod.main)


def test_tcp_bind_failure_stops_control_and_clears_capability(monkeypatch):
    import asyncio
    from datetime import datetime, timezone

    import uvicorn
    from fastapi.testclient import TestClient

    from mship.core.daemon.control import create_control_app

    class FakeConfig:
        def __init__(self, app, **kwargs):
            self.app = app
            self.host = kwargs.get("host")

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False

        async def serve(self):
            if self.config.host is not None:
                return  # TCP bind failed before Uvicorn marked the server started
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0)

    monkeypatch.setattr(uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    control_app = create_control_app(
        started_at=datetime.now(timezone.utc),
        version="1",
        socket_path="/control.sock",
        serve_bound=True,
    )

    with pytest.raises(RuntimeError, match="TCP server failed to bind"):
        run_mod._serve_forever(
            control_app,
            Path("/control.sock"),
            object(),
            {"host": "127.0.0.1", "port": 47190},
        )

    assert TestClient(control_app).get("/health").json()["capabilities"]["serve"] is False
def test_oversized_launchd_capture_preserves_latest_evidence_on_start(
    env_home, monkeypatch
):
    """Atomic rollover caps the active capture without losing crash evidence."""
    home, env = env_home
    _capture_uvicorn(monkeypatch)
    log_dir = daemon_log_dir(home)
    log_dir.mkdir(parents=True, exist_ok=True)
    capture = log_dir / "launchd.err.log"
    latest = b"latest startup traceback\n"
    capture.write_bytes(b"x" * (run_mod.LAUNCHD_CAPTURE_MAX_BYTES + 1024) + latest)
    small = log_dir / "launchd.out.log"
    small.write_text("keep me\n")

    assert run_mod.main(home=home, env=env) == 0

    assert not capture.exists()
    assert (log_dir / "launchd.err.log.1").read_bytes().endswith(latest)
    assert small.read_text() == "keep me\n"
    assert "rolled over oversized launchd capture" in (
        log_dir / "daemon.log"
    ).read_text()


def test_capture_rotated_before_logging_setup(env_home, monkeypatch):
    """A crash loop is often a BROKEN IMPORT that dies before logging is
    configured; rollover must happen first, not inside _configure_logging."""
    home, env = env_home
    order: list[str] = []
    monkeypatch.setattr(run_mod, "rotate_launchd_captures",
                        lambda d: order.append("rotate") or [])
    monkeypatch.setattr(run_mod, "_configure_logging",
                        lambda h: order.append("logging"))
    monkeypatch.setattr(run_mod, "_run", lambda h, e: order.append("run") or 0)
    assert run_mod.main(home=home, env=env) == 0
    assert order[0] == "rotate", order


# ---------------------------------------------------------------------------
# #471 Task 8: the tunnel beside the servers, and the shutdown that owns it.
# ---------------------------------------------------------------------------

RELAY_BLOCK = {"host": "relay.example"}
SERVE_BLOCK = {"host": "127.0.0.1", "port": 47190}


def _control_app():
    from datetime import datetime, timezone

    from mship.core.daemon.control import create_control_app

    return create_control_app(
        started_at=datetime.now(timezone.utc),
        version="1",
        socket_path="/control.sock",
    )


def _seed_config(home, **kwargs):
    from mship.core.daemon.registry import DaemonConfig, save_daemon_config

    save_daemon_config(home, DaemonConfig(**kwargs))


def _seed_relay_key(home):
    """A keypair on disk so nothing here spawns ssh-keygen (test_relay_link's
    `_seed_key` discipline)."""
    from mship.core.relay.keys import relay_key_path

    key = relay_key_path(home)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("PRIVATE")
    key.with_name(key.name + ".pub").write_text("ssh-ed25519 " + "A" * 68 + " mship\n")


class _FakeTunnel:
    """What the daemon knows about a tunnel: a public URL, a tick and a stop."""

    public_url = "https://hst-fake.relay.example"
    host_id = "hst-production"
    instance_id = "0123456789abcdef"

    def snapshot(self):
        return {"state": "online", "subdomain": "hst-fake"}

    def __init__(self, *, events=None, stop_after=None, servers=(), raising=False):
        self.ticks = 0
        self.stopped = False
        self._events = events if events is not None else []
        self._stop_after = stop_after
        self._servers = servers
        self._raising = raising

    def tick(self):
        # A tick after stop() means the loop was torn down without being joined
        # — the window in which a respawn forks an ssh child nothing owns.
        assert not self.stopped, "ticked after stop()"
        self.ticks += 1
        self._events.append("tick")
        if self._stop_after is not None and self.ticks >= self._stop_after:
            for server in self._servers:  # a SIGTERM lands on the servers
                server.should_exit = True
        if self._raising:
            raise RuntimeError("tick boom")

    def stop(self):
        self.stopped = True
        self._events.append("sup.stop")


@pytest.mark.parametrize(
    ("host_app", "serve_cfg", "with_tunnel", "servers"),
    [
        (None, None, False, 1),                 # control-only
        (object(), SERVE_BLOCK, False, 2),      # control + host
        (object(), SERVE_BLOCK, True, 2),       # control + host + tunnel
    ],
)
def test_serve_forever_always_runs_on_asyncio(
    monkeypatch, host_app, serve_cfg, with_tunnel, servers
):
    """All three shapes take the asyncio branch: `uvicorn.run` owns the loop
    and the signal handlers, so a shape that used it would have nowhere to run
    a tunnel and no way to stop one (the stub raises if it is reached)."""
    tunnel = _FakeTunnel() if with_tunnel else None
    _seen, built = _fake_uvicorn(monkeypatch)

    run_mod._serve_forever(
        _control_app(), Path("/control.sock"), host_app, serve_cfg, tunnel
    )

    assert len(built) == servers
    assert tunnel is None or tunnel.stopped


def test_shutdown_joins_the_tunnel_then_stops_it_then_records_clean_stop(
    env_home, monkeypatch
):
    """AC7, in order. The join COMPLETING is the first assertion: a hung gather
    would never reach the side effects, so asserting only those would pass on
    exactly the bug this shape exists to prevent — and a daemon that outlives
    `TimeoutStopSec` is SIGKILLed, leaving its ssh child holding the
    subdomain."""
    home, env = env_home
    _seed_config(home, serve=SERVE_BLOCK)
    events: list[str] = []
    _seen, servers = _fake_uvicorn(monkeypatch, hold=True)
    monkeypatch.setattr(host_tunnel_mod, "TICK_INTERVAL_S", 0)
    tunnel = _FakeTunnel(events=events, stop_after=3, servers=servers)
    monkeypatch.setattr(run_mod, "_build_tunnel", lambda *a: tunnel)
    real_clean_stop = run_mod.history.append_clean_stop
    monkeypatch.setattr(
        run_mod.history, "append_clean_stop",
        lambda *a, **k: (events.append("clean_stop"), real_clean_stop(*a, **k))[1],
    )

    rc = run_mod.main(home=home, env=env)

    assert rc == 0
    assert tunnel.ticks >= 3
    assert events[-2:] == ["sup.stop", "clean_stop"]


def test_clean_stop_is_recorded_even_when_every_tunnel_tick_raises(
    env_home, monkeypatch
):
    """The tunnel is the half of the daemon that recovers by retrying; a relay
    outage must not cost the workspaces it serves on the LAN, nor turn a clean
    shutdown into an unclean-start entry the next boot reports."""
    home, env = env_home
    _seed_config(home, serve=SERVE_BLOCK)
    _seen, servers = _fake_uvicorn(monkeypatch, hold=True)
    monkeypatch.setattr(host_tunnel_mod, "TICK_INTERVAL_S", 0)
    tunnel = _FakeTunnel(stop_after=3, servers=servers, raising=True)
    monkeypatch.setattr(run_mod, "_build_tunnel", lambda *a: tunnel)

    rc = run_mod.main(home=home, env=env)

    assert rc == 0
    assert [e.kind for e in read_history(run_mod.paths.start_history_path(home))] == [
        "start", "clean_stop",
    ]
    assert "tick boom" in (daemon_log_dir(home) / "daemon.log").read_text()


def test_unbuildable_relay_logs_and_leaves_the_daemon_healthy(env_home, monkeypatch):
    """`_build_registry`'s never-raise discipline: a relay we cannot build is a
    host without a tunnel, never a host that refuses to start."""
    home, env = env_home
    _seed_config(home, serve=SERVE_BLOCK, relay=RELAY_BLOCK)
    configs = _capture_uvicorn(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("no key material")

    monkeypatch.setattr(relay_link_mod, "RelayLink", boom)

    rc = run_mod.main(home=home, env=env)

    assert rc == 0
    # The serve bind still came up beside the control socket; only the tunnel
    # is missing.
    assert [c.kwargs.get("host") for c in configs] == [None, SERVE_BLOCK["host"]]
    assert configs[0].kwargs["uds"] == str(daemon_socket_path(env, home))
    assert [e.kind for e in read_history(run_mod.paths.start_history_path(home))] == [
        "start", "clean_stop",
    ]
    log_text = (daemon_log_dir(home) / "daemon.log").read_text()
    assert "relay tunnel unavailable" in log_text and "no key material" in log_text


def test_loser_never_builds_a_tunnel_or_spawns_ssh(
    env_home, foreign_holder, monkeypatch
):
    """A process that stands down for the incumbent must not first fork an ssh
    child onto the subdomain the incumbent is serving."""
    from mship.core.relay import tunnel as tunnel_mod

    home, env = env_home
    _seed_config(home, serve=SERVE_BLOCK, relay=RELAY_BLOCK)
    _capture_uvicorn(monkeypatch)
    spawned, built = [], []
    monkeypatch.setattr(
        tunnel_mod, "_default_proc_factory",
        lambda argv, log_path=None: spawned.append(argv),
    )
    real_build = run_mod._build_tunnel
    monkeypatch.setattr(
        run_mod, "_build_tunnel",
        lambda *a: (built.append(a), real_build(*a))[1],
    )
    monkeypatch.setattr(
        run_mod, "_probe",
        lambda sock: {"status": "ok"} if str(sock) == foreign_holder else None,
    )

    assert run_mod.main(home=home, env=env) == 0
    assert built == [] and spawned == []


def test_build_tunnel_dials_the_subdomain_the_link_currently_owns(
    tmp_path, monkeypatch
):
    """AC4's other half: an auto-reidentify moves this host to a NEW subdomain
    mid-run, and argv frozen at startup would keep re-dialing the one it no
    longer owns — reconnecting a re-identified host to somebody else's route."""
    from mship.core.relay import tunnel as tunnel_mod
    from mship.core.relay.config import RelayConfig

    class _Proc:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    home = tmp_path / "home"
    home.mkdir()
    _seed_relay_key(home)
    spawned = []
    monkeypatch.setattr(
        tunnel_mod, "_default_proc_factory",
        lambda argv, log_path=None: (spawned.append(argv), _Proc())[1],
    )

    tunnel = run_mod._build_tunnel(home, RelayConfig(host="relay.example"), SERVE_BLOCK)
    assert tunnel.host_id == tunnel._link.host_id
    assert tunnel.instance_id == tunnel._link.instance_id
    # Stands in for the auto-reidentify `test_relay_link.py` drives end to end.
    tunnel._link.subdomain = "hst-reidentified"
    tunnel._supervisor.start()

    assert spawned[-1][spawned[-1].index("-R") + 1].startswith("hst-reidentified:")
    assert str(SERVE_BLOCK["port"]) in spawned[-1][spawned[-1].index("-R") + 1]


def test_no_relay_block_means_no_tunnel(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert run_mod._relay_config(home) is None
    assert run_mod._build_tunnel(home, None, SERVE_BLOCK) is None


@pytest.mark.parametrize("server_count", [1, 2])
def test_sigterm_sets_the_shared_event_and_stops_every_server(server_count):
    """Decision (c)'s centerpiece, asserted in-process because a signal handler
    has no other honest test: uvicorn's own handlers set `should_exit` on the
    ONE server that installed them, so a tunnel loop beside them would never
    learn a stop was requested — the daemon would then outlive
    `TimeoutStopSec`, be SIGKILLed, and orphan its ssh child. Both shapes:
    control-only (1) and control+host (2)."""
    import asyncio
    import os
    import signal

    class _Server:
        def __init__(self):
            self.should_exit = False

    async def scenario():
        stop = asyncio.Event()
        servers = [_Server() for _ in range(server_count)]
        run_mod._install_stop_handlers(stop, servers)
        if signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.SIG_IGN):
            pytest.skip("no asyncio signal handlers on this platform")
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), timeout=5)
        return servers

    servers = asyncio.run(scenario())

    assert all(server.should_exit for server in servers)


def test_the_control_app_publishes_the_tunnel_it_was_built_with(env_home, monkeypatch):
    """#471 Task 9: `/health` is the ONLY reader-visible source of tunnel state
    (it lives inside this process), so a daemon that builds a tunnel and does
    not hand it to the control app renders `mship daemon status` blind."""
    from fastapi.testclient import TestClient

    home, env = env_home
    _seed_config(home, serve=SERVE_BLOCK, relay=RELAY_BLOCK)
    configs = _capture_uvicorn(monkeypatch)
    snapshot = {"state": "online", "subdomain": "hst-fake"}
    tunnel = _FakeTunnel()
    tunnel.snapshot = lambda: snapshot
    monkeypatch.setattr(run_mod, "_build_tunnel", lambda *a: tunnel)

    assert run_mod.main(home=home, env=env) == 0

    body = TestClient(configs[0].app).get("/health").json()
    assert body["capabilities"]["tunnel"] is True
    assert body["tunnel"] == snapshot


def test_production_host_app_wires_identity_tunnel_and_token_services(
    env_home, monkeypatch
):
    """The composition root must enable the host-facing seams, not merely leave
    their independently-tested defaults disabled."""
    from fastapi.testclient import TestClient

    from mship.core.daemon.host_auth import RefreshStore

    home, env = env_home
    _seed_config(home, serve=SERVE_BLOCK, relay=RELAY_BLOCK)
    configs = _capture_uvicorn(monkeypatch)
    tunnel = _FakeTunnel()
    refresh = RefreshStore(home).issue_refresh(
        host_id=tunnel.host_id, client="phone"
    )
    monkeypatch.setattr(run_mod, "_build_tunnel", lambda *a: tunnel)

    assert run_mod.main(home=home, env=env) == 0

    with TestClient(configs[1].app) as client:
        health = client.get("/health").json()
        minted = client.post("/host/token", json={"refresh": refresh})
        assert minted.status_code == 200
        authorized = client.get(
            "/workspaces",
            headers={"Authorization": f"Bearer {minted.json()['token']}"},
        )

    assert health["host_id"] == tunnel.host_id
    assert health["instance_id"] == tunnel.instance_id
    assert health["tunnel"] == tunnel.snapshot()
    assert health["runner"] == {"enabled": False, "state": "disabled"}
    assert authorized.status_code == 200
