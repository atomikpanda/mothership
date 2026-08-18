"""Daemon path derivation is pure: explicit `home` + `env`, no ambient reads."""
import os
from pathlib import Path

from mship.core.daemon.paths import (
    daemon_log_dir,
    daemon_socket_path,
    daemon_state_dir,
    lease_path,
    start_history_path,
)


def test_state_dir_and_derivatives(tmp_path: Path):
    home = tmp_path
    state = daemon_state_dir(home)
    assert state == home / ".mothership" / "daemon"
    assert daemon_log_dir(home) == state / "logs"
    assert lease_path(home) == state / "daemon.lease"
    assert start_history_path(home) == state / "start-history.json"


def test_the_suite_cannot_reach_the_real_daemon_socket(tmp_path: Path):
    """Guard for the autouse `_isolate_runtime_dir` fixture (tests/conftest.py):
    without it, any test that computes the socket path from the ambient env
    probes the operator's LIVE daemon on a box where one is running, and
    'daemon: not running' assertions fail for unrelated reasons."""
    assert str(tmp_path) in os.environ["XDG_RUNTIME_DIR"]
    assert not daemon_socket_path(os.environ, tmp_path).exists()


def test_the_suite_cannot_reach_the_real_daemon_through_the_lease_either(tmp_path: Path):
    """The second escape hatch: `probe_daemon` PREFERS the socket recorded in
    `Path.home()/.mothership/daemon/daemon.lease`, so isolating XDG_RUNTIME_DIR
    alone still lets a test that reaches the REAL home answer from the
    operator's live daemon (that is how `test_status_reports_registry_read_
    error_...` once reported `running: true` with the live pid). Probe the real
    home for real: on a box with a running mshipd this returns None only
    because the autouse sandbox refuses sockets outside the test tmp dir."""
    from pathlib import Path as _Path

    from mship.core.daemon.status import probe_daemon

    assert probe_daemon(home=_Path.home(), env=os.environ) is None
    assert probe_daemon(home=tmp_path, env=os.environ) is None


def test_socket_prefers_xdg_runtime_dir(tmp_path: Path):
    runtime = tmp_path / "runtime"
    sock = daemon_socket_path({"XDG_RUNTIME_DIR": str(runtime)}, tmp_path / "home", create=True)
    assert sock == runtime / "mship" / "daemon.sock"
    assert sock.parent.is_dir()


def test_socket_falls_back_under_state_dir(tmp_path: Path):
    home = tmp_path / "home"
    sock = daemon_socket_path({}, home, create=True)
    assert sock == daemon_state_dir(home) / "run" / "daemon.sock"
    assert sock.parent.is_dir()


def test_created_dirs_are_private(tmp_path: Path):
    home = tmp_path / "home"
    sock = daemon_socket_path({}, home, create=True)
    assert (sock.parent.stat().st_mode & 0o777) == 0o700
    runtime = tmp_path / "runtime"
    sock2 = daemon_socket_path({"XDG_RUNTIME_DIR": str(runtime)}, home, create=True)
    assert (sock2.parent.stat().st_mode & 0o777) == 0o700


def test_default_is_pure_no_dirs_created(tmp_path: Path):
    """Probe/status paths must not create dirs: a stale/unwritable
    XDG_RUNTIME_DIR would crash `mship daemon status` (agentic review P2)."""
    runtime = tmp_path / "runtime"
    sock = daemon_socket_path({"XDG_RUNTIME_DIR": str(runtime)}, tmp_path / "home")
    assert sock == runtime / "mship" / "daemon.sock"
    assert not sock.parent.exists()


def test_unwritable_runtime_dir_does_not_raise_by_default(tmp_path: Path):
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    sock = daemon_socket_path({"XDG_RUNTIME_DIR": str(blocked / "rt")}, tmp_path / "home")
    assert sock.name == "daemon.sock"  # computed, nothing raised
