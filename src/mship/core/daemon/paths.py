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


def host_identity_path(home: Path) -> Path:
    return daemon_state_dir(home) / "host-identity.json"


def host_secret_path(home: Path) -> Path:
    """The per-host root secret (`core.daemon.host_token`). Read-only — creates
    nothing, so a reporter can check for it without minting one."""
    return daemon_state_dir(home) / "host-root-secret"


def host_tokens_path(home: Path) -> Path:
    return daemon_state_dir(home) / "host-tokens.json"


def host_refresh_path(home: Path) -> Path:
    return daemon_state_dir(home) / "host-refresh.json"


def tunnel_log_path(home: Path) -> Path:
    """Captured `ssh -R` output for the HOST tunnel (#471).

    Daemon-owned and per-OS-user, deliberately NOT the workspace-scoped
    `.mothership/relay-tunnel.log` that `mship serve --relay` writes: the daemon
    dials one tunnel for the machine, not one per workspace, and it must not
    write into a workspace it happens to have discovered."""
    return daemon_log_dir(home) / "relay-tunnel.log"


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
