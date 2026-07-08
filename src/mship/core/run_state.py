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

Concurrency is per-item files, committed and pushed with a bounded fetch + rebase
+ re-push retry on a non-fast-forward:

* Writers on **different** items touch different files, so the rebase merges
  cleanly and every writer eventually pushes.
* Writers on the **same** claim file deliberately *conflict* — that conflict IS
  the exclusivity. The loser does not error: its rebase is aborted, it re-syncs,
  and it stands down (``try_claim`` returns the winner's ``ClaimInfo``, per the
  method contract). Only genuinely unexpected git failures raise ``RunStateError``.

Error contract: every public method either succeeds or raises ``RunStateError``
(git failures are normalized to it — no raw ``CalledProcessError`` leaks out);
corrupt on-ref data (a bad claim JSON or a partial run-log line) is tolerated and
skipped, mirroring the inbox lease.
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
_MAX_ATTEMPTS = 5  # bounded re-evaluation/rebase rounds under concurrent writers
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "mship",
    "GIT_AUTHOR_EMAIL": "mship@local",
    "GIT_COMMITTER_NAME": "mship",
    "GIT_COMMITTER_EMAIL": "mship@local",
}


class RunStateError(RuntimeError):
    """A git operation against the run-state ref failed unrecoverably."""


class _RebaseConflict(Exception):
    """Internal: a push lost a race and the rebase hit a same-file conflict.

    Never escapes the public API — each mutator catches it, re-syncs to the
    winner's state, and re-evaluates (stand down or retry)."""


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
        holder's ClaimInfo when another run already holds it (caller stands down).

        Under a concurrent same-item race the losing writer's push conflicts; it
        re-syncs and re-evaluates rather than erroring, so a caller doing
        ``if claim is None: run() else: stand_down()`` is always safe."""
        for _ in range(_MAX_ATTEMPTS):
            self._sync()
            existing = self._read_claim(item_id)
            if not self._reclaimable(existing, holder, now):
                return existing  # a live, foreign holder → stand down
            self._write_claim(item_id, ClaimInfo(holder=holder, heartbeat_at=now))
            try:
                self._commit_and_push(f"claim {item_id} by {holder}")
                return None  # we won
            except _RebaseConflict:
                continue  # someone raced us; re-sync and re-evaluate
        # Exhausted contention rounds: report whoever holds it now (never a false win).
        self._sync()
        existing = self._read_claim(item_id)
        if existing is not None:
            return existing
        raise RunStateError(f"claim contention for {item_id!r} exceeded {_MAX_ATTEMPTS} rounds")

    def refresh(self, item_id: str, holder: str, now: datetime) -> None:
        """Advance the heartbeat while we still hold it (no-op if someone else does)."""
        for _ in range(_MAX_ATTEMPTS):
            self._sync()
            existing = self._read_claim(item_id)
            if existing is None or existing.holder != holder:
                return  # not ours anymore → nothing to heartbeat
            self._write_claim(item_id, ClaimInfo(holder=holder, heartbeat_at=now))
            try:
                self._commit_and_push(f"refresh {item_id} by {holder}")
                return
            except _RebaseConflict:
                continue
        # a lost heartbeat is non-fatal (we simply look stale) → give up quietly

    def release(self, item_id: str, holder: str) -> None:
        """Release the claim, but only if `holder` currently holds it. A displaced
        holder's release must not clobber a claim another run reclaimed."""
        for _ in range(_MAX_ATTEMPTS):
            self._sync()
            existing = self._read_claim(item_id)
            if existing is None or existing.holder != holder:
                return  # already gone or reclaimed by someone else → no-op
            self._claim_path(item_id).unlink(missing_ok=True)
            try:
                self._commit_and_push(f"release {item_id} by {holder}")
                return
            except _RebaseConflict:
                continue

    def read_claim(self, item_id: str) -> ClaimInfo | None:
        self._sync()
        return self._read_claim(item_id)

    def append_log(self, item_id: str, text: str, now: datetime) -> None:
        line = json.dumps({"text": text, "at": now.isoformat()})
        for _ in range(_MAX_ATTEMPTS):
            self._sync()  # adopt any concurrent appends before re-adding our line
            path = self._log_path(item_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(line + "\n")
            try:
                self._commit_and_push(f"log {item_id}: {text[:60]}")
                return
            except _RebaseConflict:
                continue
        raise RunStateError(f"append_log contention for {item_id!r} exceeded {_MAX_ATTEMPTS} rounds")

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
            try:
                rec = json.loads(raw)
                entries.append(LogEntry(text=rec["text"], at=datetime.fromisoformat(rec["at"])))
            except (ValueError, KeyError, TypeError):
                continue  # corrupt/partial line → skip (mirrors read_claim tolerance)
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
        """Run a git command in the workdir. With ``check=True`` a nonzero exit is
        normalized to ``RunStateError`` (never a raw ``CalledProcessError``) so the
        module presents a single error type to callers."""
        proc = subprocess.run(
            ["git", *args],
            cwd=self._workdir,
            capture_output=True,
            text=True,
            env={**os.environ, **_COMMIT_ENV},
        )
        if check and proc.returncode != 0:
            raise RunStateError(f"git {args[0]} failed: {proc.stderr or proc.stdout}")
        return proc

    def _ensure_init(self) -> None:
        if (self._workdir / ".git").exists():
            return
        self._workdir.mkdir(parents=True, exist_ok=True)
        # Start life directly on the orphan branch (unborn, no shared history with
        # the workspace's other branches).
        init = subprocess.run(
            ["git", "init", "-q", "-b", self._branch, str(self._workdir)],
            capture_output=True, text=True,
        )
        if init.returncode != 0:
            raise RunStateError(f"git init failed: {init.stderr or init.stdout}")
        self._git("remote", "add", "origin", self._origin, check=True)

    def _sync(self) -> None:
        """Adopt the latest state of the orphan ref from origin (clean checkout)."""
        self._ensure_init()
        if self._git("fetch", "origin", self._branch).returncode == 0:
            self._git("reset", "-q", "--hard", "FETCH_HEAD", check=True)
            self._git("clean", "-qfd")  # drop any leftover untracked scratch
        # else: ref absent on origin yet → stay on the unborn orphan branch (empty).

    def _commit_and_push(self, message: str) -> None:
        """Commit the staged working tree and push to the orphan ref.

        On a non-fast-forward, fetch + rebase + re-push, up to ``_MAX_ATTEMPTS``
        rounds (concurrent writers on *different* items each land eventually). If a
        rebase hits a conflict — a concurrent write to the *same* file — abort it
        and raise ``_RebaseConflict`` for the caller to re-evaluate."""
        self._git("add", "-A", check=True)
        commit = self._git("commit", "-q", "-m", message)
        if commit.returncode != 0:
            if "nothing to commit" in (commit.stdout + commit.stderr):
                return  # a no-op write → nothing to push
            raise RunStateError(f"commit failed: {commit.stderr or commit.stdout}")
        for _ in range(_MAX_ATTEMPTS):
            if self._push().returncode == 0:
                return
            # push rejected (non-fast-forward / lock contention) → integrate + retry
            self._git("fetch", "origin", self._branch, check=True)
            rebase = self._git("rebase", "FETCH_HEAD")
            if rebase.returncode != 0:
                self._git("rebase", "--abort")
                raise _RebaseConflict(message)  # same-file race → caller stands down/retries
        raise RunStateError(f"push rejected after {_MAX_ATTEMPTS} rebase retries: {message}")

    def _push(self) -> subprocess.CompletedProcess:
        return self._git("push", "-q", "origin", f"HEAD:{self._branch}")
