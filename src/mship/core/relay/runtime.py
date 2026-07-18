"""Per-workspace serve runtime record — how `mship pair` auto-discovers the relay
host of a running `mship serve --relay-host <host>` (spec mship-pair-relay-host).

`mship serve --relay` writes `<workspace>/.mothership/relay-runtime.json` (mode
0600; `.mothership/` is already gitignored) while it is relaying and unlinks it on
shutdown. It carries only the relay host (no secret — the token stays in
`serve-token`) plus liveness/debug fields. `mship pair` reads it, ignores a record
whose pid is dead, and resolves the relay host with a fixed precedence.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from mship.core.relay.config import RelayConfig

RECORD_NAME = "relay-runtime.json"


@dataclass(frozen=True)
class RelayRuntimeRecord:
    """What a running relay-serve persists so another process (pair) can discover it.

    Only `host` is strictly needed to rebuild the link (pair recomputes subdomain +
    token deterministically); `pid` gates staleness; the rest is for debugging /
    `mship relay whoami`.
    """
    host: str
    pid: int
    subdomain: str | None = None
    url: str | None = None
    workspace: str | None = None
    ssh_port: int = 2222
    user: str | None = None


def _record_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / ".mothership" / RECORD_NAME


def write_runtime_record(workspace_root: Path, record: RelayRuntimeRecord) -> None:
    """Persist `record` as 0600 JSON at `<workspace_root>/.mothership/relay-runtime.json`.

    Created 0600 from the outset — no world-readable window between create and
    chmod (the record carries subdomain/workspace/user, so a `write_text` + later
    `chmod` would leak them to any local process during the TOCTOU gap)."""
    path = _record_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(record))
    # Remove any prior file so O_CREAT's 0o600 mode always applies (O_CREAT does
    # NOT re-mode an existing file), then create + write in one shot.
    path.unlink(missing_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload)


def read_runtime_record(workspace_root: Path) -> RelayRuntimeRecord | None:
    """Return the persisted record, or None if absent, unreadable, corrupt, or
    missing required keys (never raises — a bad record must never break pair)."""
    path = _record_path(workspace_root)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        # OSError covers absent/unreadable/permission-denied; ValueError covers
        # corrupt JSON. A bad record must NEVER raise (the docstring's contract) —
        # it just means "no discoverable relay", so pair falls back cleanly.
        return None
    if not isinstance(data, dict) or "host" not in data or "pid" not in data:
        return None
    try:
        return RelayRuntimeRecord(
            host=data["host"],
            pid=int(data["pid"]),
            subdomain=data.get("subdomain"),
            url=data.get("url"),
            workspace=data.get("workspace"),
            ssh_port=int(data.get("ssh_port", 2222)),
            user=data.get("user"),
        )
    except (TypeError, ValueError):
        return None


def clear_runtime_record(workspace_root: Path) -> None:
    """Remove the record (idempotent — no error when already gone)."""
    _record_path(workspace_root).unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` exists (signal 0 probes without killing)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except (OverflowError, ValueError):
        return False  # nonsense pid → treat as not alive
    return True


def live_runtime_record(
    workspace_root: Path,
    *,
    pid_alive: Callable[[int], bool] | None = None,
) -> RelayRuntimeRecord | None:
    """The runtime record ONLY when present AND its pid is alive; else None.

    A stale record (serve stopped / crashed / pid reused-away) is treated as absent
    so pair never derives a link from a dead serve. `pid_alive` is injectable for
    tests; the CLI leaves it None → module-level `_pid_alive` (so a test can also
    monkeypatch `mship.core.relay.runtime._pid_alive`).
    """
    record = read_runtime_record(workspace_root)
    if record is None:
        return None
    alive = (pid_alive or _pid_alive)(record.pid)
    return record if alive else None


@dataclass(frozen=True)
class ResolvedRelay:
    host: str
    ssh_port: int
    user: str | None
    source: str  # "flag" | "config" | "record"


def resolve_relay(
    *,
    flag_host: str | None,
    config_relay: RelayConfig | None,
    record: RelayRuntimeRecord | None,
) -> ResolvedRelay | None:
    """Resolve the relay host with fixed precedence: flag > config > live record.

    `record` should already be liveness-filtered (see `live_runtime_record`) — this
    function is pure precedence. Returns None when nothing resolves (caller emits an
    actionable error). An explicit `flag_host` overrides host but inherits
    ssh_port/user from `config_relay` when present, mirroring the RelayConfig
    substitution in `_serve_with_relay` (serve.py:197-201).
    """
    if flag_host:
        if config_relay is not None:
            return ResolvedRelay(
                host=flag_host,
                ssh_port=config_relay.ssh_port,
                user=config_relay.user,
                source="flag",
            )
        return ResolvedRelay(host=flag_host, ssh_port=2222, user=None, source="flag")
    if config_relay is not None:
        return ResolvedRelay(
            host=config_relay.host,
            ssh_port=config_relay.ssh_port,
            user=config_relay.user,
            source="config",
        )
    if record is not None:
        return ResolvedRelay(
            host=record.host, ssh_port=record.ssh_port, user=record.user, source="record"
        )
    return None
