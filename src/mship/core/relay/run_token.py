from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mship.core.relay.grants import Scope

_ID_RE = re.compile(r"\A[0-9a-f]{1,32}\Z")


@dataclass(frozen=True)
class RunToken:
    token_id: str
    enrollment_id: str
    scope: Scope


def _dir(base_dir) -> Path:
    d = Path(base_dir) / "run-tokens"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def issue_run_token(
    base_dir, *, enrollment_id: str, scope: Scope, ttl_seconds: int,
    clock: Callable[[], float] = time.time,
) -> str:
    """Mint a per-run token, persist only its hash, return `<token_id>.<secret>`
    (the plaintext — printed once by the caller; never re-derivable)."""
    d = _dir(base_dir)
    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    rec = {
        "token_id": token_id,
        "enrollment_id": enrollment_id,
        "repos": list(scope.repos),
        "push_branch": scope.push_branch,
        "secret_hash": _hash(secret),
        "expires_at": clock() + ttl_seconds,
    }
    path = d / f"{token_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec))
    tmp.replace(path)
    return f"{token_id}.{secret}"


def verify_run_token(
    base_dir, presented: str, *, clock: Callable[[], float] = time.time,
) -> RunToken | None:
    """Return the RunToken for a valid, unexpired presented token, else None."""
    if "." not in presented:
        return None
    token_id, secret = presented.split(".", 1)
    if not _ID_RE.match(token_id):
        return None
    path = _dir(base_dir) / f"{token_id}.json"
    try:
        rec = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not hmac.compare_digest(_hash(secret), rec.get("secret_hash", "")):
        return None
    if clock() >= rec.get("expires_at", 0):
        return None
    return RunToken(
        token_id=token_id,
        enrollment_id=rec["enrollment_id"],
        scope=Scope(repos=tuple(rec.get("repos", [])), push_branch=rec.get("push_branch")),
    )
