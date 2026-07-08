"""Single-listener lease for the message inbox (enforces one-agent-per-workspace).

The `receiving-messages` skill assumes exactly one agent drains a workspace's
shared mailbox. When two `claude` sessions in the same workspace both arm
`mship inbox wait`, every phone message wakes both and gets answered twice
(duplicate replies / decision cards). This lease makes the invariant enforced,
not just documented: a second `inbox wait` that finds a live lease stands down
instead of racing.

The lease is a small JSON file (`<state_dir>/inbox-listener.lock`) holding the
holder pid + a heartbeat the active waiter refreshes each poll. It is reclaimable
when the holder process is gone or its heartbeat is older than the TTL, so a
crashed or exited listener never wedges the mailbox. The acquire critical section
takes an advisory flock (same primitive as state.lock) so two simultaneous
acquires can't both win.
"""
from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class LeaseInfo:
    pid: int
    heartbeat_at: datetime


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` exists (signal 0 probes without killing)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


class InboxLease:
    """Reclaimable single-holder lease. `try_acquire` returns None on success or
    the live holder's LeaseInfo when another agent already holds it."""

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: float = 10.0,
        pid_alive: Callable[[int], bool] = _pid_alive,
    ) -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._pid_alive = pid_alive

    def read(self) -> LeaseInfo | None:
        try:
            raw = json.loads(self._path.read_text())
            return LeaseInfo(
                pid=int(raw["pid"]),
                heartbeat_at=datetime.fromisoformat(raw["heartbeat_at"]),
            )
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            return None  # missing or corrupt → treat as unheld

    def _reclaimable(self, info: LeaseInfo | None, me: int, now: datetime) -> bool:
        if info is None or info.pid == me:
            return True
        if (now - info.heartbeat_at).total_seconds() > self._ttl:
            return True  # heartbeat went stale → holder is gone or wedged
        return not self._pid_alive(info.pid)

    def _write(self, pid: int, now: datetime) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps({"pid": pid, "heartbeat_at": now.isoformat()}))
        tmp.replace(self._path)

    def try_acquire(self, pid: int, now: datetime) -> LeaseInfo | None:
        """Take the lease (or take over a dead/stale one). Returns None on
        success, or the live holder's LeaseInfo if another agent holds it."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        guard = self._path.with_name(self._path.name + ".flock")
        with open(guard, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                info = self.read()
                if not self._reclaimable(info, pid, now):
                    return info  # live, fresh, different holder → caller stands down
                self._write(pid, now)
                return None
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def refresh(self, pid: int, now: datetime) -> None:
        """Heartbeat while we still hold it (no-op if another holder took over)."""
        info = self.read()
        if info is None or info.pid == pid:
            self._write(pid, now)

    def release(self, pid: int) -> None:
        """Best-effort release on exit; only removes our own lease."""
        info = self.read()
        if info is not None and info.pid == pid:
            self._path.unlink(missing_ok=True)
