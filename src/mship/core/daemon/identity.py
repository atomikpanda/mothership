"""Host identity (#471): who this machine IS, independent of any workspace.

Three distinct things, deliberately:

- `host_id` — minted once (`hst-<ts>-<uuid8>`), persisted, and the name the
  relay directory keys on. Never derived from the relay key: that key is a
  FILE, and a cloned VM reproduces it byte-for-byte.
- machine fingerprint — a best-effort binding to the physical/virtual machine
  (`/etc/machine-id`, DMI product uuid). Catches a RE-IMAGED host. It does NOT
  catch `cp -a`/snapshot clones, which copy the fingerprint verbatim — that is
  net (b)'s job (the relay arbitrates by probing the incumbent).
- `instance_id` — minted per PROCESS, in memory, never written to disk. A clone
  cannot copy what was never persisted, so it is what lets the relay tell a
  restart from a second live claimant.

Everything here is workspace-free: no config, no cwd, no repos. The daemon is
one per host and its identity must not depend on which workspaces it serves.
"""
from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Sequence

_FINGERPRINT_SOURCES = (
    Path("/etc/machine-id"),
    Path("/var/lib/dbus/machine-id"),
    Path("/sys/class/dmi/id/product_uuid"),
)


@dataclass(frozen=True)
class HostIdentity:
    host_id: str
    created_at: str
    fingerprint: str | None = None
    cloned_from: str | None = None
    reidentified: bool = False
    adopted_fingerprint: str | None = None


def mint_host_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"hst-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


def mint_instance_id() -> str:
    """Per-process, in-memory only. Deliberately NOT persisted: a clone must
    not be able to reproduce it (see module docstring)."""
    return secrets.token_hex(8)


def machine_fingerprint(readers: Sequence[Path] | None = None) -> str | None:
    """Best-effort machine binding, or None where unavailable (containers).

    None is NOT a mismatch signal — an unreadable fingerprint must never raise
    a false clone alarm on a host that simply cannot report one.
    """
    for path in (readers if readers is not None else _FINGERPRINT_SOURCES):
        try:
            value = Path(path).read_text().strip()
        except OSError:
            continue
        if value:
            return value
    return None


def _write(path: Path, ident: HostIdentity) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps({
            "host_id": ident.host_id,
            "created_at": ident.created_at,
            "fingerprint": ident.fingerprint,
            "cloned_from": ident.cloned_from,
        }).encode())
    finally:
        os.close(fd)
    tmp.replace(path)


def _read(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict) or not raw.get("host_id"):
            return None
        return raw
    except (OSError, ValueError):
        return None  # corrupt/truncated → re-mint (RegistryStore._load_nolock precedent)


def _rotate_relay_key(home: Path) -> None:
    """Move the current relay key aside so a re-identified host presents a NEW
    key: the clone's copied key is still in the relay's `pubkeys/`, so keeping
    it would let the clone keep authenticating as this host."""
    from mship.core.relay.keys import relay_key_path

    key = relay_key_path(home)
    for path in (key, key.with_name(key.name + ".pub")):
        if path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            path.replace(path.with_name(f"{path.name}.pre-reidentify-{stamp}"))


def ensure_host_identity(
    home: Path,
    *,
    fingerprint: str | None = None,
    on_mismatch: Literal["reidentify", "keep"] = "reidentify",
    rotate_key: Callable[[Path], None] = _rotate_relay_key,
    now: datetime | None = None,
) -> HostIdentity:
    """Load or mint this host's identity.

    A recorded fingerprint that no longer matches the running machine means the
    identity file travelled to different hardware (a re-imaged/restored host):
    with `on_mismatch="reidentify"` (default) a NEW host_id is minted, the old
    one recorded as `cloned_from`, and the relay key rotated so the twin cannot
    keep authenticating as us. `on_mismatch="keep"` is the operator's explicit
    "this is still the same host" adoption.

    Idempotent: a second call returns the same identity, and a re-identified
    host does not re-identify again on the next call.
    """
    from mship.core.daemon.paths import host_identity_path

    path = host_identity_path(home)
    raw = _read(path)
    if raw is None:
        ident = HostIdentity(
            host_id=mint_host_id(now),
            created_at=(now or datetime.now(timezone.utc)).isoformat(),
            fingerprint=fingerprint,
        )
        _write(path, ident)
        return ident

    recorded = raw.get("fingerprint")
    mismatch = (
        recorded is not None and fingerprint is not None and recorded != fingerprint
    )
    if not mismatch:
        return HostIdentity(
            host_id=raw["host_id"],
            created_at=raw.get("created_at", ""),
            fingerprint=recorded,
            cloned_from=raw.get("cloned_from"),
        )

    if on_mismatch == "keep":
        ident = HostIdentity(
            host_id=raw["host_id"],
            created_at=raw.get("created_at", ""),
            fingerprint=fingerprint,
            cloned_from=raw.get("cloned_from"),
            adopted_fingerprint=fingerprint,
        )
        _write(path, ident)
        return ident

    rotate_key(home)
    ident = HostIdentity(
        host_id=mint_host_id(now),
        created_at=(now or datetime.now(timezone.utc)).isoformat(),
        fingerprint=fingerprint,
        cloned_from=raw["host_id"],
        reidentified=True,
    )
    _write(path, ident)
    return ident
