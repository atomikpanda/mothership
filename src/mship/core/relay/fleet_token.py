"""Per-device fleet tokens (#471): the credential a phone carries to read the
relay's host directory (`GET /hosts`).

The `core.daemon.host_auth.RefreshStore` shape, for the same two reasons:

- **Stable per label.** `mship relay fleet-token --label phone` is the command
  that prints the QR, and an operator will run it again just to re-display it.
  A fresh secret on every run would silently unpair the phone that already
  scanned the last one — so the secret is *derived* from a relay root secret
  plus a per-label nonce, and a re-mint recomputes it and writes nothing.
- **Revocation is real.** `revoke` drops the record *and its nonce*, so a later
  re-mint under the same label derives a different credential rather than
  resurrecting the one the operator just killed.

Nothing is stored in plaintext: only a sha256 of the derived secret, which is
what `verify` compares against. Verification is a pure read — a phone polling
the directory must not churn the store.
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
from pathlib import Path

from mship.core.relay.keys import ensure_secret_file

# Bounded so the document cannot grow without limit; far above any real fleet
# (this counts *devices the owner paired*, not hosts).
MAX_FLEET_LABELS = 32

_ROOT_SECRET_LEN = 32

# Record keys are the first 16 hex of a sha256 over the label, so a label is
# never a path component and never a lookup key verbatim.
_LABEL_ID_LEN = 16
_ID_RE = re.compile(r"\A[0-9a-f]{%d}\Z" % _LABEL_ID_LEN)

# A label is an operator-typed nickname ("phone"); bounded so it cannot be used
# to bloat the document.
MAX_LABEL_LEN = 64


def _label_id(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:_LABEL_ID_LEN]


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _private_dir(path: Path) -> None:
    """Owner-only parent dir. The corrective chmod is not redundant: mkdir's
    mode is masked by the umask, and `exist_ok=True` never tightens a dir that
    already exists too loose (the `registry.py`/`host_token.py` house pattern)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)


@contextmanager
def _locked(lock_path: Path, mode: int):
    """Advisory flock (the `registry.py`/`host_auth.py` house pattern)."""
    _private_dir(lock_path)
    lock_path.touch(exist_ok=True, mode=0o600)
    with open(lock_path, "r+") as lf:
        fcntl.flock(lf, mode)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


class FleetTokenStore:
    """Flock'd RMW over one JSON doc of per-label fleet-token records."""

    def __init__(self, store_dir, *, max_labels: int = MAX_FLEET_LABELS) -> None:
        self._dir = Path(store_dir)
        self.path = self._dir / "fleet-tokens.json"
        self._lock = self.path.with_name(self.path.name + ".lock")
        self._secret_path = self._dir / "fleet-secret"
        self._max_labels = max_labels

    # -- storage -----------------------------------------------------------

    def _load(self) -> dict:
        """Records by label_id; a corrupt/truncated doc reads as empty (every
        token it held is unverifiable anyway — `RegistryStore._load_nolock`)."""
        try:
            doc = json.loads(self.path.read_text())
            labels = doc["labels"]
            return labels if isinstance(labels, dict) else {}
        except OSError, ValueError, KeyError, TypeError:
            return {}

    def _save(self, labels: dict) -> None:
        """Atomic, owner-only write: mkstemp creates the temp file 0600, so the
        hashes are never briefly world-readable."""
        _private_dir(self.path)
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", dir=self.path.parent
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(json.dumps({"labels": labels}, indent=2))
            os.replace(temp, self.path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        self.path.chmod(0o600)

    def _derive(self, label: str, nonce: str) -> str:
        """The secret for one record — recomputable, so a re-mint returns the
        same string without ever storing the plaintext."""
        root = ensure_secret_file(self._secret_path, _ROOT_SECRET_LEN)
        return hmac.new(
            root, f"{label}\x00{nonce}".encode("utf-8"), hashlib.sha256
        ).hexdigest()

    # -- API ---------------------------------------------------------------

    def issue(self, label: str) -> str:
        """This label's fleet token (`<label_id>.<secret>`), minted only if it
        has none. Re-running the mint command is a no-op on disk."""
        label = (label or "").strip()
        if not label or len(label) > MAX_LABEL_LEN:
            raise ValueError(f"label must be 1..{MAX_LABEL_LEN} characters")
        label_id = _label_id(label)
        with _locked(self._lock, fcntl.LOCK_EX):
            labels = self._load()
            existing = labels.get(label_id)
            replacing = isinstance(existing, dict) and bool(existing.get("nonce"))
            if replacing:
                secret = self._derive(label, existing["nonce"])
                if hmac.compare_digest(
                    _hash(secret), str(existing.get("secret_hash", ""))
                ):
                    return f"{label_id}.{secret}"
            # Evict from the PRE-EXISTING set only, so the credential we are
            # about to hand back can never be the one the cap drops. Replacing
            # a record whose root-derived secret no longer matches needs no slot.
            if not replacing and len(labels) >= self._max_labels:
                labels = dict(
                    sorted(
                        labels.items(),
                        key=lambda kv: (
                            kv[1].get("created_at", 0) if isinstance(kv[1], dict) else 0
                        ),
                        reverse=True,
                    )[: max(self._max_labels - 1, 0)]
                )
            nonce = secrets.token_hex(16)
            secret = self._derive(label, nonce)
            labels[label_id] = {
                "label_id": label_id,
                "label": label,
                "nonce": nonce,
                "secret_hash": _hash(secret),
                "created_at": time.time(),
            }
            self._save(labels)
        return f"{label_id}.{secret}"

    def verify(self, presented: str) -> str | None:
        """The label behind a live token, else None. Never raises, never writes."""
        if not presented or "." not in presented:
            return None
        label_id, secret = presented.split(".", 1)
        if not _ID_RE.match(label_id):
            return None
        with _locked(self._lock, fcntl.LOCK_SH):
            rec = self._load().get(label_id)
        if not isinstance(rec, dict):
            return None
        if not hmac.compare_digest(_hash(secret), str(rec.get("secret_hash", ""))):
            return None
        return str(rec.get("label", ""))

    def revoke(self, label: str) -> bool:
        """Drop this label's credential. True if one was there to drop."""
        label_id = _label_id((label or "").strip())
        with _locked(self._lock, fcntl.LOCK_EX):
            labels = self._load()
            if label_id not in labels:
                return False
            labels.pop(label_id)
            self._save(labels)
        return True
