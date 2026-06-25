from __future__ import annotations
import base64
import hashlib
import json
import os
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


# Request ids are secrets.token_hex(16). Reject anything else before it touches a
# filesystem path — defense-in-depth against a crafted id like "../../evil".
_RID_RE = re.compile(r"\A[0-9a-f]{1,64}\Z")


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

    def _read_rec(self, p: Path) -> dict | None:
        """Load a pending record, quarantining a corrupt/truncated file instead of raising.

        A hand-edited or partially-written `pending/*.json` must not brick the whole
        store (one bad file would otherwise poison every sweep/list/get). On failure the
        file is moved aside to `*.json.corrupt` and skipped."""
        try:
            rec = json.loads(p.read_text())
            if "created_at" not in rec:
                raise ValueError("missing created_at")
            return rec
        except (json.JSONDecodeError, OSError, ValueError):
            try:
                p.replace(p.with_suffix(".json.corrupt"))
            except OSError:
                p.unlink(missing_ok=True)
            return None

    def _sweep(self) -> None:
        now = self._clock()
        for p in list(self._pending.glob("*.json")):
            rec = self._read_rec(p)
            if rec is None:
                continue
            if now - rec["created_at"] >= self._ttl:
                self._resolve(p, rec, "expired")

    def create(self, pubkey: str, hostname: str) -> str:
        # The store is the security boundary, not just the HTTP layer: self-protect.
        # validate_pubkey rejects multi-line/CRLF input, so a crafted second line can't
        # be smuggled in here and later written into the pubkeys allowlist on approve.
        if not validate_pubkey(pubkey):
            raise ValueError("invalid pubkey")
        self._sweep()
        if len(list(self._pending.glob("*.json"))) >= self._max_pending:
            raise PendingCapReached()
        rid = secrets.token_hex(16)
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
        out = []
        for p in sorted(self._pending.glob("*.json")):
            rec = self._read_rec(p)
            if rec is not None:
                out.append(rec)
        return out

    def get(self, rid: str) -> str:
        if not _RID_RE.match(rid):
            return "unknown"
        self._sweep()
        if (self._pending / f"{rid}.json").exists():
            return "pending"
        rec = self._read_rec(self._resolved / f"{rid}.json")
        return rec.get("status", "unknown") if rec else "unknown"

    def approve(self, rid: str, pubkeys_dir) -> None:
        if not _RID_RE.match(rid):
            raise NotPending(rid)
        self._sweep()
        p = self._pending / f"{rid}.json"
        if not p.exists():
            raise NotPending(rid)
        rec = self._read_rec(p)
        if rec is None:                       # corrupt/truncated → quarantined, treat as gone
            raise NotPending(rid)
        dest = _unique_pub_path(Path(pubkeys_dir), sanitize_label(rec["hostname"]))
        # Atomic key write: sish re-reads pubkeys/ per connection and must never observe
        # a half-written key file mid-write.
        tmp = dest.with_suffix(".pub.tmp")
        tmp.write_text(rec["pubkey"] + "\n")
        tmp.replace(dest)
        self._resolve(p, rec, "approved")

    def deny(self, rid: str) -> None:
        if not _RID_RE.match(rid):
            raise NotPending(rid)
        # Sweep first so denying an already-expired request resolves it as `expired`,
        # consistent with the other methods.
        self._sweep()
        p = self._pending / f"{rid}.json"
        if not p.exists():
            raise NotPending(rid)
        rec = self._read_rec(p)
        if rec is None:                       # corrupt/truncated → quarantined, treat as gone
            raise NotPending(rid)
        self._resolve(p, rec, "denied")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _unique_pub_path(pubkeys_dir: Path, stem: str) -> Path:
    """Atomically reserve a not-yet-existing `<stem>[-N].pub` in pubkeys_dir.

    Uses O_CREAT|O_EXCL to claim the filename race-free — two concurrent approvals
    for the same hostname get distinct files and neither silently overwrites the
    other (an exists()-then-write check would have a TOCTOU window). The caller
    overwrites the empty reserved file with the key content via tmp+replace."""
    pubkeys_dir.mkdir(parents=True, exist_ok=True)
    i = 1
    while True:
        cand = pubkeys_dir / (f"{stem}.pub" if i == 1 else f"{stem}-{i}.pub")
        try:
            os.close(os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return cand
        except FileExistsError:
            i += 1
            if i > 1000:
                raise RuntimeError("too many key files for this label")
