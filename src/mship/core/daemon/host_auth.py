"""Refresh credentials (#471): one long-lived, revocable credential per client.

The phone holds one of these and trades it for short-lived bearers
(`host_token`). Two properties matter and neither is free:

- **Stable per `(host_id, client)` (AC11).** A network flap re-registers, and
  re-registration must re-publish the *same* credential — otherwise N
  reconnects mint N credentials, the file grows, and the phone's copy becomes
  one of many. Since nothing is stored in plaintext, "the same credential"
  cannot mean "read it back": it is *derived* from the host root secret and a
  per-record nonce, so a re-issue recomputes it and writes nothing at all.
- **Revocation is real.** `revoke` drops the record (and its nonce), so a later
  re-registration of the same client name derives a *different* credential
  rather than resurrecting the one the operator just killed.

Verification is a pure read; only issue/revoke write.
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
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mship.core.daemon.host_token import ensure_host_root_secret
from mship.core.daemon.paths import host_refresh_path
from mship.core.relay.token_clock import is_expired

# Long-lived by design (it is the thing that survives reboots and flaps), but
# not forever: a phone that has not come back in a month re-pairs.
REFRESH_TTL_S = 30 * 86_400

MAX_REFRESH_CLIENTS = 32

# Client ids are exactly the first 16 hex chars of a sha256 digest
# (`_client_id`); anything else is rejected before it is used as a lookup key.
_CLIENT_ID_LEN = 16
_ID_RE = re.compile(r"\A[0-9a-f]{%d}\Z" % _CLIENT_ID_LEN)


@dataclass(frozen=True)
class RefreshGrant:
    client_id: str
    client: str
    host_id: str


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


def _client_id(host_id: str, client: str) -> str:
    """A stable, traversal-proof record key for one client of one host."""
    return hashlib.sha256(f"{host_id}\x00{client}".encode("utf-8")).hexdigest()[:16]


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class RefreshStore:
    """Flock'd RMW over one JSON doc of per-client refresh records."""

    def __init__(
        self,
        home: Path,
        *,
        ttl_seconds: float = REFRESH_TTL_S,
        max_clients: int = MAX_REFRESH_CLIENTS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._home = Path(home)
        self._path = host_refresh_path(self._home)
        self._lock = self._path.with_name(self._path.name + ".lock")
        self._ttl = ttl_seconds
        self._max_clients = max_clients
        self._clock = clock

    # -- storage ----------------------------------------------------------

    def _load(self) -> dict:
        """Records by client_id; a corrupt/truncated doc reads as empty (its
        records are unverifiable anyway — `RegistryStore._load_nolock`)."""
        try:
            doc = json.loads(self._path.read_text())
            clients = doc["clients"]
            return clients if isinstance(clients, dict) else {}
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def _save(self, clients: dict) -> None:
        """Atomic, owner-only write (`host_app._atomic_write_owner_file` shape):
        mkstemp creates the temp file 0600, so the hashes are never briefly
        world-readable."""
        _private_dir(self._path)
        fd, temp_name = tempfile.mkstemp(
            prefix=self._path.name + ".", dir=self._path.parent
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(json.dumps({"clients": clients}, indent=2))
            os.replace(temp, self._path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        self._path.chmod(0o600)

    def _live(self, clients: dict) -> dict:
        now = self._clock()
        return {
            cid: rec
            for cid, rec in clients.items()
            if isinstance(rec, dict) and not is_expired(rec.get("expires_at", 0), now)
        }

    # -- credential derivation --------------------------------------------

    def _derive(self, host_id: str, client: str, nonce: str) -> str:
        """The credential secret for one record — recomputable, so a re-issue
        returns the same string without ever storing the plaintext."""
        root = ensure_host_root_secret(self._home)
        msg = f"{host_id}\x00{client}\x00{nonce}".encode("utf-8")
        return hmac.new(root, msg, hashlib.sha256).hexdigest()

    # -- API ---------------------------------------------------------------

    def issue_refresh(self, *, host_id: str, client: str) -> str:
        """Return this client's refresh credential, minting it only if it has
        none. Re-registration is a no-op on disk (AC11)."""
        client_id = _client_id(host_id, client)
        with _locked(self._lock, fcntl.LOCK_EX):
            clients = self._load()
            live = self._live(clients)
            existing = live.get(client_id)
            if existing is not None and existing.get("nonce"):
                if live != clients:  # expired siblings to drop; else touch nothing
                    self._save(live)
                return f"{client_id}.{self._derive(host_id, client, existing['nonce'])}"
            # No usable record (absent, expired, or hand-corrupted) → mint one.
            # Evict from the PRE-EXISTING set only, so the credential we are
            # about to hand back can never be the one the cap drops.
            if len(live) >= self._max_clients:
                live = dict(sorted(
                    live.items(),
                    key=lambda kv: kv[1].get("created_at", 0),
                    reverse=True,
                )[: max(self._max_clients - 1, 0)])

            nonce = secrets.token_hex(16)
            secret = self._derive(host_id, client, nonce)
            now = self._clock()
            live[client_id] = {
                "client_id": client_id,
                "client": client,
                "host_id": host_id,
                "nonce": nonce,
                "secret_hash": _hash(secret),
                "created_at": now,
                "expires_at": now + self._ttl,
            }
            self._save(live)
        return f"{client_id}.{secret}"

    def verify_refresh(self, presented: str) -> RefreshGrant | None:
        """Return the grant behind a valid, unrevoked, unexpired credential,
        else None. Never raises, and never writes."""
        if "." not in presented:
            return None
        client_id, secret = presented.split(".", 1)
        if not _ID_RE.match(client_id):
            return None
        with _locked(self._lock, fcntl.LOCK_SH):
            rec = self._load().get(client_id)
        if not isinstance(rec, dict):
            return None
        if not hmac.compare_digest(_hash(secret), str(rec.get("secret_hash", ""))):
            return None
        if is_expired(rec.get("expires_at", 0), self._clock()):
            return None
        return RefreshGrant(
            client_id=client_id,
            client=rec.get("client", ""),
            host_id=rec.get("host_id", ""),
        )

    def revoke(self, *, host_id: str, client: str) -> bool:
        """Drop this client's credential. True if one was there to drop."""
        client_id = _client_id(host_id, client)
        with _locked(self._lock, fcntl.LOCK_EX):
            clients = self._load()
            if client_id not in clients:
                return False
            # Prune here too: this is a write, so pay off the expired records
            # rather than re-persisting them.
            remaining = self._live(clients)
            remaining.pop(client_id, None)
            self._save(remaining)
        return True
