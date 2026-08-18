"""Daemon filesystem layout — pure functions over explicit `home`/`env`.

All daemon state is per-OS-user (`~/.mothership/daemon/`, beside the per-machine
relay identity in `mship.core.relay.keys`), never per-workspace: the daemon is
workspace-agnostic until #472. Injectable-`home` style follows `relay/keys.py`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping


def daemon_state_dir(home: Path) -> Path:
    return home / ".mothership" / "daemon"


def daemon_log_dir(home: Path) -> Path:
    return daemon_state_dir(home) / "logs"


def lease_path(home: Path) -> Path:
    return daemon_state_dir(home) / "daemon.lease"


def start_history_path(home: Path) -> Path:
    return daemon_state_dir(home) / "start-history.json"


def daemon_config_path(home: Path) -> Path:
    return daemon_state_dir(home) / "config.yaml"


def registry_path(home: Path) -> Path:
    return daemon_state_dir(home) / "workspaces.json"


def daemon_socket_path(env: Mapping[str, str], home: Path, *, create: bool = False) -> Path:
    """Control-socket path: `$XDG_RUNTIME_DIR/mship/daemon.sock`, else a 0700
    dir under the state dir (macOS, some containers). NOTE: the daemon and a
    probing CLI can legitimately compute different paths when their
    environments differ (systemd-provided XDG_RUNTIME_DIR vs a bare shell) —
    probes must prefer the socket_path recorded in the lease (`status.py`).
    """
    runtime = env.get("XDG_RUNTIME_DIR")
    if runtime:
        sock_dir = Path(runtime) / "mship"
    else:
        sock_dir = daemon_state_dir(home) / "run"
    if create:
        # Only the daemon itself creates the dir; probe/status paths must stay
        # pure — a stale/unwritable XDG_RUNTIME_DIR in an SSH env would
        # otherwise crash `mship daemon status` with PermissionError.
        sock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        sock_dir.chmod(0o700)
    return sock_dir / "daemon.sock"
