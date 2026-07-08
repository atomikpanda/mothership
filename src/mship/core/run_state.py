"""Git-backed run-state store: per-item run-claims + an append-only run-log.

Ephemeral cloud runs (a Claude routine, cron+`claude -p`, CI) have no always-on
server to coordinate through, so the run-shared state lives on a dedicated
**orphan branch** in the workspace repo's origin (`mship-run-state`, spec q1),
committed and pushed as checkpoints. Any run that can push to origin can see it.

Layout on the ref::

    claims/<item_id>.json   {"holder": "...", "heartbeat_at": "<iso>"}
    logs/<item_id>.jsonl    one {"text": "...", "at": "<iso>"} per line

The claim is exactly the inbox-listener lease generalized: an in-process pid+probe
becomes an opaque holder token + heartbeat, and reclaimability is the shared
`core/lease_common.is_reclaimable` rule (unheld / mine / stale-heartbeat →
reclaimable; there is no liveness probe for a remote token, so staleness stands in
for "holder gone"). See `core/inbox_lease.py`, which shares the same helper.

Concurrency: writes are per-item files, committed and pushed with a single
pull --rebase + re-push retry on a non-fast-forward. Because concurrent runs touch
different per-item files, the rebase merges cleanly.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mship.core.lease_common import is_reclaimable

_BRANCH_DEFAULT = "mship-run-state"
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "mship",
    "GIT_AUTHOR_EMAIL": "mship@local",
    "GIT_COMMITTER_NAME": "mship",
    "GIT_COMMITTER_EMAIL": "mship@local",
}


class RunStateError(RuntimeError):
    """A git operation against the run-state ref failed unrecoverably."""


@dataclass(frozen=True)
class ClaimInfo:
    holder: str
    heartbeat_at: datetime


@dataclass(frozen=True)
class LogEntry:
    text: str
    at: datetime


def _safe_name(item_id: str) -> str:
    """Filesystem-safe leaf for an item id (ids are slug-like, but be defensive)."""
    return item_id.replace("/", "_").replace("\\", "_").replace("..", "_")


class RunStateRepo:
    """Git-backed run-claim + run-log for one workspace origin.

    ``origin`` is any git URL/path (a bare repo in tests); ``workdir`` is a local
    checkout this instance owns. Every read/write first syncs the orphan ref from
    origin, so separate runs (separate workdirs) share state through git.
    """

    def __init__(
        self,
        origin,
        workdir,
        *,
        branch: str = _BRANCH_DEFAULT,
        ttl_seconds: float = 1800,
    ) -> None:
        self._origin = str(origin)
        self._workdir = Path(workdir)
        self._branch = branch
        self._ttl = ttl_seconds

    # ---- public API -------------------------------------------------------

    def try_claim(self, item_id: str, holder: str, now: datetime) -> ClaimInfo | None:
        """Claim `item_id` for `holder`. Returns None on success, or the live
        holder's ClaimInfo when another run already holds it (caller stands down)."""
        self._sync()
        existing = self._read_claim(item_id)
        if not self._reclaimable(existing, holder, now):
            return existing
        self._write_claim(item_id, ClaimInfo(holder=holder, heartbeat_at=now))
        self._commit_and_push(f"claim {item_id} by {holder}")
        return None

    def refresh(self, item_id: str, holder: str, now: datetime) -> None:
        """Advance the heartbeat while we still hold it (no-op if someone else does)."""
        self._sync()
        existing = self._read_claim(item_id)
        if existing is None or existing.holder != holder:
            return
        self._write_claim(item_id, ClaimInfo(holder=holder, heartbeat_at=now))
        self._commit_and_push(f"refresh {item_id} by {holder}")

    def release(self, item_id: str, holder: str) -> None:
        """Release the claim, but only if `holder` currently holds it. A displaced
        holder's release must not clobber a claim another run reclaimed."""
        self._sync()
        existing = self._read_claim(item_id)
        if existing is None or existing.holder != holder:
            return
        self._claim_path(item_id).unlink(missing_ok=True)
        self._commit_and_push(f"release {item_id} by {holder}")

    def read_claim(self, item_id: str) -> ClaimInfo | None:
        self._sync()
        return self._read_claim(item_id)

    def append_log(self, item_id: str, text: str, now: datetime) -> None:
        self._sync()
        path = self._log_path(item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"text": text, "at": now.isoformat()})
        with path.open("a") as fh:
            fh.write(line + "\n")
        self._commit_and_push(f"log {item_id}: {text[:60]}")

    def read_log(self, item_id: str) -> list[LogEntry]:
        self._sync()
        path = self._log_path(item_id)
        if not path.exists():
            return []
        entries: list[LogEntry] = []
        for raw in path.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            rec = json.loads(raw)
            entries.append(LogEntry(text=rec["text"], at=datetime.fromisoformat(rec["at"])))
        return entries

    # ---- reclaim rule (shared with inbox_lease) ---------------------------

    def _reclaimable(self, existing: ClaimInfo | None, me: str, now: datetime) -> bool:
        # No liveness probe for an opaque remote token → staleness alone.
        return is_reclaimable(
            holder_identity=None if existing is None else existing.holder,
            heartbeat_at=None if existing is None else existing.heartbeat_at,
            me=me,
            now=now,
            ttl_seconds=self._ttl,
            is_alive=None,
        )

    # ---- paths ------------------------------------------------------------

    def _claim_path(self, item_id: str) -> Path:
        return self._workdir / "claims" / f"{_safe_name(item_id)}.json"

    def _log_path(self, item_id: str) -> Path:
        return self._workdir / "logs" / f"{_safe_name(item_id)}.jsonl"

    def _read_claim(self, item_id: str) -> ClaimInfo | None:
        path = self._claim_path(item_id)
        try:
            rec = json.loads(path.read_text())
            return ClaimInfo(
                holder=str(rec["holder"]),
                heartbeat_at=datetime.fromisoformat(rec["heartbeat_at"]),
            )
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return None  # missing or corrupt → treat as unheld

    def _write_claim(self, item_id: str, info: ClaimInfo) -> None:
        path = self._claim_path(item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "holder": info.holder,
            "heartbeat_at": info.heartbeat_at.isoformat(),
        }))

    # ---- git plumbing -----------------------------------------------------

    def _git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self._workdir,
            capture_output=True,
            text=True,
            check=check,
            env={**os.environ, **_COMMIT_ENV},
        )

    def _ensure_init(self) -> None:
        if (self._workdir / ".git").exists():
            return
        self._workdir.mkdir(parents=True, exist_ok=True)
        # Start life directly on the orphan branch (unborn, no shared history with
        # the workspace's other branches).
        subprocess.run(
            ["git", "init", "-q", "-b", self._branch, str(self._workdir)],
            check=True, capture_output=True, text=True,
        )
        self._git("remote", "add", "origin", self._origin, check=True)

    def _sync(self) -> None:
        """Adopt the latest state of the orphan ref from origin (clean checkout)."""
        self._ensure_init()
        if self._git("fetch", "origin", self._branch).returncode == 0:
            self._git("reset", "-q", "--hard", "FETCH_HEAD", check=True)
            self._git("clean", "-qfd")  # drop any leftover untracked scratch
        # else: ref absent on origin yet → stay on the unborn orphan branch (empty).

    def _commit_and_push(self, message: str) -> None:
        self._git("add", "-A", check=True)
        commit = self._git("commit", "-q", "-m", message)
        if commit.returncode != 0:
            # nothing staged (a no-op write) → nothing to push
            if "nothing to commit" in (commit.stdout + commit.stderr):
                return
            raise RunStateError(f"commit failed: {commit.stderr or commit.stdout}")
        if self._push().returncode == 0:
            return
        # non-fast-forward (or transient) → one pull --rebase + re-push
        if self._git("fetch", "origin", self._branch).returncode == 0:
            rebase = self._git("rebase", "FETCH_HEAD")
            if rebase.returncode != 0:
                self._git("rebase", "--abort")
                raise RunStateError(f"rebase after non-fast-forward failed: {rebase.stderr}")
        retry = self._push()
        if retry.returncode != 0:
            raise RunStateError(f"push failed after rebase retry: {retry.stderr}")

    def _push(self) -> subprocess.CompletedProcess:
        return self._git("push", "-q", "origin", f"HEAD:{self._branch}")
