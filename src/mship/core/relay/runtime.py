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
    """Persist `record` as 0600 JSON at `<workspace_root>/.mothership/relay-runtime.json`."""
    path = _record_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record)))
    path.chmod(0o600)


def read_runtime_record(workspace_root: Path) -> RelayRuntimeRecord | None:
    """Return the persisted record, or None if absent, unreadable, corrupt, or
    missing required keys (never raises — a bad record must never break pair)."""
    path = _record_path(workspace_root)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
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
