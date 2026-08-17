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


def _capture_uvicorn(monkeypatch):
    seen = {}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **k: seen.update(k))
    return seen


def test_main_acquires_lease_then_serves_socket(env_home, monkeypatch):
    home, env = env_home
    seen = _capture_uvicorn(monkeypatch)
    lease_seen_held_by_uvicorn = {}

    import uvicorn

    real_update = seen.update

    def fake_run(app, **k):
        # By the time uvicorn is entered, lease + history must already exist.
        lease_seen_held_by_uvicorn["lease"] = json.loads(lease_path(home).read_text())
        lease_seen_held_by_uvicorn["history"] = [e.kind for e in read_history(run_mod.paths.start_history_path(home))]
        real_update(k)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    rc = run_mod.main(home=home, env=env)
    assert rc == 0
    assert seen["uds"] == str(daemon_socket_path(env, home))
    assert "host" not in seen and "port" not in seen
    assert seen["log_config"] is None
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
    assert called == {}  # uvicorn never entered
    assert any("already running" in r.message for r in caplog.records)


def test_lost_race_with_dead_holder_exits_nonzero(env_home, foreign_holder, monkeypatch):
    home, env = env_home
    called = _capture_uvicorn(monkeypatch)
    monkeypatch.setattr(run_mod, "_probe", lambda sock: None)  # holder never answers
    rc = run_mod.main(home=home, env=env)
    assert rc != 0
    assert called == {}


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
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **k: (_ for _ in ()).throw(RuntimeError("boom")))
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


def test_oversized_launchd_capture_is_truncated_on_start(env_home, monkeypatch):
    """launchd never rotates StandardErrorPath: under KeepAlive relaunch a
    persistent startup failure would grow it forever, so the daemon caps it."""
    home, env = env_home
    _capture_uvicorn(monkeypatch)
    log_dir = daemon_log_dir(home)
    log_dir.mkdir(parents=True, exist_ok=True)
    big = log_dir / "launchd.err.log"
    big.write_bytes(b"x" * (run_mod._LAUNCHD_CAPTURE_MAX_BYTES + 1024))
    small = log_dir / "launchd.out.log"
    small.write_text("keep me\n")

    assert run_mod.main(home=home, env=env) == 0

    assert big.stat().st_size == 0            # capped
    assert small.read_text() == "keep me\n"   # under the cap: untouched
    assert "truncated oversized launchd capture" in (log_dir / "daemon.log").read_text()
