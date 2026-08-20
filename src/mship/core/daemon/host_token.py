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
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from mship.core.daemon.paths import host_secret_path, host_tokens_path
from mship.core.relay.host_contract import HOST_TOKEN_TTL_S
from mship.core.relay.keys import ensure_secret_file
from mship.core.relay.token_clock import AnchoredClock, Deadline

# HOST_TOKEN_TTL_S is deliberately short — a host token is the credential a
# phone carries, and the only revocation that survives a lost/stolen device is
# expiry (revocation happens one tier up, on the refresh credential in
# `host_auth`). It is OWNED by `core.relay.host_contract` because both ends of
# the wire need it, and re-exported here so the mint site reads as one name.

# Bounded so the doc cannot grow without limit while every token is still live
# (many devices, or a client re-minting in a tight loop).
MAX_HOST_TOKENS = 64

_ROOT_SECRET_LEN = 32

# Token ids are exactly `secrets.token_hex(8)` — 16 lowercase hex. Anything
# else is rejected before it is used as a lookup key: defense in depth against
# a crafted `../../evil` id ever reaching a path or a record.
_ID_RE = re.compile(r"\A[0-9a-f]{16}\Z")


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


def _private_dir(path: Path) -> None:
    """Owner-only parent dir. The corrective chmod is not redundant: mkdir's
    mode is masked by the umask, and `exist_ok=True` never tightens a dir that
    already exists too loose (the `registry.py`/`host_app.py` house pattern)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)


@contextmanager
def _locked(lock_path: Path, mode: int):
    """Advisory flock (the `registry.py`/`state.py` house pattern)."""
    _private_dir(lock_path)
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
    """Atomic, owner-only write (`host_app._atomic_write_owner_file` shape):
    mkstemp creates the temp file 0600, so the hashes are never briefly
    world-readable."""
    _private_dir(path)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps({"tokens": tokens}, indent=2))
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


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
        # Evict from the PRE-EXISTING set only: the token we are about to hand
        # back must never be the one the cap drops (a short-TTL mint at the cap
        # sorts last and would be issued already dead).
        if len(tokens) >= max_tokens:
            tokens = dict(sorted(
                tokens.items(),
                key=lambda kv: kv[1].get("expires_at", 0),
                reverse=True,
            )[: max(max_tokens - 1, 0)])
        tokens[token_id] = {
            "token_id": token_id,
            "secret_hash": _hash(secret),
            **clock.deadline(ttl_seconds).as_record(),
        }
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
    rec = _load(path).get(token_id)
    if not isinstance(rec, dict):
        return None
    if not hmac.compare_digest(_hash(secret), str(rec.get("secret_hash", ""))):
        return None
    if _expired(rec, clock):
        return None
    return HostToken(token_id=token_id, expires_at=float(rec["expires_at"]))
