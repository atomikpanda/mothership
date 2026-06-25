from __future__ import annotations
import base64
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from typing import Callable


def _b64decode_strict(body: str) -> bytes:
    """Decode base64, adding padding if needed, rejecting non-base64 characters."""
    padding = (4 - len(body) % 4) % 4
    padded = body + "=" * padding
    return base64.b64decode(padded, validate=True)


def validate_pubkey(s: str) -> bool:
    """True if `s` is a single ssh public-key line (key-type + decodable base64 body).

    Checks *shape* only — a recognized key-type prefix plus a base64-decodable body
    on a single line. It does not parse the SSH wire format or verify cryptographic
    key structure; the real security boundary for enrollment is owner approval, not
    this function. The single-line guard rejects multi-line / CRLF input so a crafted
    second line cannot be smuggled into the authorized_keys allowlist on approval.
    """
    s = s.strip()
    if "\n" in s or "\r" in s:  # reject multi-line / CRLF authorized_keys injection
        return False
    parts = s.split()
    if len(parts) < 2:
        return False
    ktype, body = parts[0], parts[1]
    if not ktype.startswith(("ssh-", "ecdsa-", "sk-")):
        return False
    try:
        _b64decode_strict(body)
    except Exception:
        return False
    return len(body) >= 20


def fingerprint(pubkey: str) -> str:
    """ssh-keygen-style SHA256 fingerprint of the key body: `SHA256:<base64-no-pad>`."""
    parts = pubkey.strip().split()
    if len(parts) < 2:
        raise ValueError("not an ssh public key")
    body = parts[1]
    digest = hashlib.sha256(_b64decode_strict(body)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def sanitize_label(hostname: str) -> str:
    """A safe pubkeys-filename stem from a hostname: lowercase [a-z0-9-], no traversal, ≤40."""
    s = re.sub(r"[^a-z0-9]+", "-", (hostname or "").lower()).strip("-")[:40].strip("-")
    return s or "device"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PendingCapReached(Exception):
    """Too many simultaneously-pending requests."""


class NotPending(Exception):
    """No pending request with that id (unknown, already resolved, or expired)."""


# ---------------------------------------------------------------------------
# RequestStore
# ---------------------------------------------------------------------------


class RequestStore:
    """Filesystem-backed enroll requests: pending/<id>.json, moved to resolved/ on
    approve/deny/expire. Atomic writes; lazy TTL expiry on every read/mutate."""

    def __init__(
        self,
        base_dir,
        ttl_seconds: int = 1800,
        max_pending: int = 50,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._pending = Path(base_dir) / "pending"
        self._resolved = Path(base_dir) / "resolved"
        self._pending.mkdir(parents=True, exist_ok=True)
        self._resolved.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_seconds
        self._max_pending = max_pending
        self._clock = clock

    def _write_atomic(self, path: Path, rec: dict) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec))
        tmp.replace(path)

    def _resolve(self, p: Path, rec: dict, status: str) -> None:
        rec = dict(rec)
        rec["status"] = status
        self._write_atomic(self._resolved / p.name, rec)
        p.unlink(missing_ok=True)

    def _sweep(self) -> None:
        now = self._clock()
        for p in list(self._pending.glob("*.json")):
            rec = json.loads(p.read_text())
            if now - rec["created_at"] >= self._ttl:
                self._resolve(p, rec, "expired")

    def create(self, pubkey: str, hostname: str) -> str:
        self._sweep()
        if len(list(self._pending.glob("*.json"))) >= self._max_pending:
            raise PendingCapReached()
        rid = secrets.token_hex(4)
        self._write_atomic(
            self._pending / f"{rid}.json",
            {
                "id": rid,
                "pubkey": pubkey.strip(),
                "hostname": hostname,
                "fingerprint": fingerprint(pubkey),
                "created_at": self._clock(),
                "status": "pending",
            },
        )
        return rid

    def list_pending(self) -> list[dict]:
        self._sweep()
        return [json.loads(p.read_text()) for p in sorted(self._pending.glob("*.json"))]

    def get(self, rid: str) -> str:
        self._sweep()
        if (self._pending / f"{rid}.json").exists():
            return "pending"
        r = self._resolved / f"{rid}.json"
        if r.exists():
            return json.loads(r.read_text())["status"]
        return "unknown"

    def approve(self, rid: str, pubkeys_dir) -> None:
        self._sweep()
        p = self._pending / f"{rid}.json"
        if not p.exists():
            raise NotPending(rid)
        rec = json.loads(p.read_text())
        dest = _unique_pub_path(Path(pubkeys_dir), sanitize_label(rec["hostname"]))
        dest.write_text(rec["pubkey"] + "\n")
        self._resolve(p, rec, "approved")

    def deny(self, rid: str) -> None:
        p = self._pending / f"{rid}.json"
        if not p.exists():
            raise NotPending(rid)
        self._resolve(p, json.loads(p.read_text()), "denied")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _unique_pub_path(pubkeys_dir: Path, stem: str) -> Path:
    """Return a Path that doesn't yet exist in pubkeys_dir, using stem with a counter suffix."""
    pubkeys_dir.mkdir(parents=True, exist_ok=True)
    cand = pubkeys_dir / f"{stem}.pub"
    i = 2
    while cand.exists():
        cand = pubkeys_dir / f"{stem}-{i}.pub"
        i += 1
    return cand
