"""Daemon path derivation is pure: explicit `home` + `env`, no ambient reads."""
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


def test_socket_prefers_xdg_runtime_dir(tmp_path: Path):
    runtime = tmp_path / "runtime"
    sock = daemon_socket_path({"XDG_RUNTIME_DIR": str(runtime)}, tmp_path / "home")
    assert sock == runtime / "mship" / "daemon.sock"
    assert sock.parent.is_dir()


def test_socket_falls_back_under_state_dir(tmp_path: Path):
    home = tmp_path / "home"
    sock = daemon_socket_path({}, home)
    assert sock == daemon_state_dir(home) / "run" / "daemon.sock"
    assert sock.parent.is_dir()


def test_created_dirs_are_private(tmp_path: Path):
    home = tmp_path / "home"
    sock = daemon_socket_path({}, home)
    assert (sock.parent.stat().st_mode & 0o777) == 0o700
    runtime = tmp_path / "runtime"
    sock2 = daemon_socket_path({"XDG_RUNTIME_DIR": str(runtime)}, home)
    assert (sock2.parent.stat().st_mode & 0o777) == 0o700
