"""Short-lived host bearer tokens (#471): `<token_id>.<secret>`, hash at rest.

Modelled on `core.relay.run_token`, with two deliberate differences:

- **One JSON doc, not a file per token**, because these are minted often and
  must be prunable/cappable as a set (a per-file store grows unbounded until
  something sweeps it, and nothing would).
- **Expiry is delegated to `core.relay.token_clock`**, not re-derived here.
  These bearers live and die on a long-running VM whose wall clock gets
  stepped, so the deadline carries an epoch-tagged monotonic floor (AC10).

Verification is a pure read: a reconnecting phone must not churn the store
(AC11). Only issuance writes, and it prunes and caps while it holds the lock.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from mship.core.daemon.paths import host_secret_path, host_tokens_path
from mship.core.relay.keys import ensure_secret_file
from mship.core.relay.token_clock import AnchoredClock, Deadline

# Deliberately short: a host token is the credential a phone carries, and the
# only revocation that survives a lost/stolen device is expiry (revocation
# happens one tier up, on the refresh credential in `host_auth`).
HOST_TOKEN_TTL_S = 300

# Bounded so the doc cannot grow without limit while every token is still live
# (many devices, or a client re-minting in a tight loop).
MAX_HOST_TOKENS = 64

_ROOT_SECRET_LEN = 32

# Token ids are `secrets.token_hex(8)`. Anything else is rejected before it is
# used as a lookup key — defense in depth against a crafted `../../evil` id.
_ID_RE = re.compile(r"\A[0-9a-f]{1,32}\Z")


@dataclass(frozen=True)
class HostToken:
    token_id: str
    expires_at: float


def ensure_host_root_secret(home: Path) -> bytes:
    """This host's root key material, generated on first use (0600).

    Not used to hash bearer tokens (those are random and stored as a plain
    sha256) — it exists so `host_auth` can *derive* a stable per-client refresh
    credential instead of storing one in plaintext (AC11).
    """
    return ensure_secret_file(host_secret_path(home), _ROOT_SECRET_LEN)


@contextmanager
def _locked(lock_path: Path, mode: int):
    """Advisory flock (the `registry.py`/`state.py` house pattern)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path.touch(exist_ok=True, mode=0o600)
    with open(lock_path, "r+") as lf:
        fcntl.flock(lf, mode)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _load(path: Path) -> dict:
    """Records by token_id; a corrupt/truncated doc reads as empty (every token
    it held is unverifiable anyway — `RegistryStore._load_nolock` precedent)."""
    try:
        doc = json.loads(path.read_text())
        tokens = doc["tokens"]
        return tokens if isinstance(tokens, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _save(path: Path, tokens: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"tokens": tokens}, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _expired(rec: dict, clock: AnchoredClock) -> bool:
    return clock.is_expired(Deadline.from_record(rec))


def issue_host_token(
    home: Path,
    *,
    ttl_seconds: float = HOST_TOKEN_TTL_S,
    clock: AnchoredClock | None = None,
    max_tokens: int = MAX_HOST_TOKENS,
) -> str:
    """Mint a bearer, persist only its hash and deadline, return the plaintext
    `<token_id>.<secret>` — handed out once and never re-derivable."""
    clock = clock or AnchoredClock()
    path = host_tokens_path(home)
    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    with _locked(path.with_name(path.name + ".lock"), fcntl.LOCK_EX):
        tokens = {
            tid: rec for tid, rec in _load(path).items() if not _expired(rec, clock)
        }
        tokens[token_id] = {
            "token_id": token_id,
            "secret_hash": _hash(secret),
            **clock.deadline(ttl_seconds).as_record(),
        }
        if len(tokens) > max_tokens:
            keep = sorted(
                tokens.items(), key=lambda kv: kv[1]["expires_at"], reverse=True
            )[:max_tokens]
            tokens = dict(keep)
        _save(path, tokens)
    return f"{token_id}.{secret}"


def verify_host_token(
    home: Path, presented: str, *, clock: AnchoredClock | None = None
) -> HostToken | None:
    """Return the HostToken for a valid, unexpired bearer, else None. Never
    raises, and never writes."""
    clock = clock or AnchoredClock()
    if "." not in presented:
        return None
    token_id, secret = presented.split(".", 1)
    if not _ID_RE.match(token_id):
        return None
    path = host_tokens_path(home)
    with _locked(path.with_name(path.name + ".lock"), fcntl.LOCK_SH):
        rec = _load(path).get(token_id)
    if not isinstance(rec, dict):
        return None
    if not hmac.compare_digest(_hash(secret), str(rec.get("secret_hash", ""))):
        return None
    if _expired(rec, clock):
        return None
    return HostToken(token_id=token_id, expires_at=float(rec["expires_at"]))
