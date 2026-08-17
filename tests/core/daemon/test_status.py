"""Status assembly: all inputs injected; the lease file is read-only JSON
diagnostics on this path — never opened for locking."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

import mship.core.daemon.status as status_mod
from mship.core.daemon.history import HistoryEntry
from mship.core.daemon.lease import LeaseInfo
from mship.core.daemon.status import build_status, restart_blockers
from mship.core.daemon.supervisor import SupervisorState

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)

HEALTH = {
    "status": "ok",
    "pid": 4242,
    "mship_version": "0.5.52",
    "protocol": 1,
    "started_at": "2026-08-16T11:00:00+00:00",
    "uptime_s": 3600.0,
    "socket": "/run/user/1000/mship/daemon.sock",
    "capabilities": {"serve": False, "tunnel": False, "registry": False, "runner": False},
}


def _status(**kw):
    defaults = dict(
        supervisor_state=SupervisorState("active"),
        linger="yes",
        lease_info=LeaseInfo(pid=4242, socket_path="/run/user/1000/mship/daemon.sock"),
        health=HEALTH,
        cli_version="0.5.52",
        history_entries=[],
        now=NOW,
    )
    defaults.update(kw)
    return build_status(**defaults)


def test_healthy_rendering():
    s = _status()
    assert s.running is True
    assert s.pid == 4242
    assert s.daemon_version == "0.5.52"
    assert s.compatible is True
    assert s.supervised is True
    rendered = s.render()
    assert "tunnel: not configured (#471)" in rendered
    assert "workspaces: registry pending (#472)" in rendered
    assert "runner: not configured (#473)" in rendered


def test_version_skew_detected():
    s = _status(health={**HEALTH, "mship_version": "0.5.51"}, cli_version="0.5.52")
    assert s.compatible is False
    assert "restart required: daemon v0.5.51, CLI v0.5.52" in s.render()


def test_absent_rendering():
    """No lease record + failed probe + inactive supervisor → absent. A stale
    lease alone never reads as 'already running'."""
    s = _status(lease_info=None, health=None, supervisor_state=SupervisorState("absent"))
    assert s.running is False
    assert "not running" in s.render()


def test_unresponsive_rendering():
    s = _status(health=None)
    assert s.running is False
    assert "unresponsive" in s.render()


def test_crash_loop_rendering():
    entries = [HistoryEntry("start", NOW.replace(minute=57 - i)) for i in range(3)]
    s = _status(history_entries=entries)
    assert "3 unclean starts in last 10m" in s.render()


def test_healthy_but_unsupervised_warns():
    """The normal result of `mship daemon run` in a shell: never render plain
    'healthy', which would pretend persistence exists."""
    s = _status(supervisor_state=SupervisorState("absent"))
    assert s.running is True
    assert s.supervised is False
    rendered = s.render()
    assert "outside the supervisor" in rendered
    assert "will not survive" in rendered


def test_linger_off_warns():
    s = _status(linger="no")
    assert "linger" in s.render().lower()
    assert "warning" in s.render().lower()


def test_probe_uses_lease_socket_path(tmp_path, monkeypatch):
    """XDG_RUNTIME_DIR can differ between the daemon's systemd-provided env and
    the invoking shell — the probe must hit the LEASE's recorded path, else a
    healthy daemon renders 'unresponsive' purely from env divergence."""
    probed = {}

    def fake_probe(sock, **kw):
        probed["path"] = str(sock)
        return HEALTH

    monkeypatch.setattr(status_mod, "probe_control_socket", fake_probe)
    home = tmp_path
    lease_file = home / ".mothership" / "daemon" / "daemon.lease"
    lease_file.parent.mkdir(parents=True)
    lease_file.write_text(
        '{"pid": 1, "started_at": "2026-08-16T11:00:00+00:00", "version": "1", "socket_path": "/daemon/env/mship.sock"}'
    )
    payload = status_mod.probe_daemon(home=home, env={"XDG_RUNTIME_DIR": str(tmp_path / "cli-env")})
    assert payload == HEALTH
    assert probed["path"] == "/daemon/env/mship.sock"

    # No lease at all → falls back to the computed path.
    lease_file.unlink()
    status_mod.probe_daemon(home=home, env={"XDG_RUNTIME_DIR": str(tmp_path / "cli-env")})
    assert probed["path"] == str(tmp_path / "cli-env" / "mship" / "daemon.sock")


def test_status_never_touches_the_lease_flock(tmp_path, monkeypatch):
    """Liveness is the socket probe + supervisor state; a status probe that
    transiently flocks the lease can race a starting daemon into its loser
    path."""
    import fcntl

    def forbidden(*a, **kw):  # pragma: no cover - failure path
        raise AssertionError("status path acquired a lock on the lease file")

    monkeypatch.setattr(fcntl, "flock", forbidden)
    monkeypatch.setattr(status_mod, "probe_control_socket", lambda sock, **kw: None)
    home = tmp_path
    lease_file = home / ".mothership" / "daemon" / "daemon.lease"
    lease_file.parent.mkdir(parents=True)
    lease_file.write_text('{"pid": 1, "socket_path": "/x.sock"}')
    status_mod.probe_daemon(home=home, env={})


def test_restart_blockers_empty_in_v1():
    assert restart_blockers() == []


def test_single_unclean_start_is_not_labeled_crash_loop():
    """One kill -9 is informational, not an alarm: the crash-loop label is
    gated on history.is_crash_looping's threshold."""
    entries = [HistoryEntry("start", NOW.replace(minute=58))]
    s = _status(history_entries=entries)
    rendered = s.render()
    assert "crash loop" not in rendered
    assert "unclean starts: 1 in last 10m" in rendered
