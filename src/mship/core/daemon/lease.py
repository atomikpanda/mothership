"""Single-instance daemon lease: a lifetime-held flock on the lease file.

Contract (#470): THE HELD FLOCK IS THE LIVENESS AUTHORITY; the recorded pid is
diagnostic only. This is deliberately stronger than `inbox_lease._pid_alive`'s
signal-0 probe — a recycled pid must never read as "daemon running".

The lease JSON `{pid, started_at, version, socket_path}` doubles as the runtime
record. It is written IN PLACE through the locked fd (ftruncate + write +
fsync): a tmp+os.replace write is explicitly wrong here because os.replace
swaps the inode, stranding the lifetime flock on the old unlinked inode so
every later acquirer flocks the fresh file and "wins", voiding the guard
(`test_late_arrival_after_write_loses`).

No CLI/status path may instantiate this class: a status probe that transiently
flocks the lease can race a starting daemon into its loser path. Status reads
the JSON only (`status.py`).
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class LeaseInfo:
    """Holder diagnostics read from the lease record. `pid is None` means
    "held by unknown" — the flock is held but the record was unreadable
    (holder mid-write)."""

    pid: int | None
    started_at: str | None = None
    version: str | None = None
    socket_path: str | None = None


_READ_RETRIES = 5
_READ_RETRY_DELAY_S = 0.03


class DaemonLease:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def try_acquire(self, *, version: str, socket_path: str) -> LeaseInfo | None:
        """Win → write the record through the locked fd, keep the flock for the
        process lifetime, return None. Lose → return the holder's LeaseInfo
        (pid=None when the record stays unreadable after brief retries)."""
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return self._read_holder()
        # Won: record ourselves through the SAME fd — never replace the file.
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "version": version,
                "socket_path": socket_path,
            }
        ).encode()
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.fsync(fd)
        self._fd = fd
        return None

    def _read_holder(self) -> LeaseInfo:
        for attempt in range(_READ_RETRIES):
            try:
                raw = json.loads(self._path.read_text())
                return LeaseInfo(
                    pid=int(raw["pid"]),
                    started_at=raw.get("started_at"),
                    version=raw.get("version"),
                    socket_path=raw.get("socket_path"),
                )
            except (OSError, ValueError, KeyError, TypeError):
                if attempt < _READ_RETRIES - 1:
                    time.sleep(_READ_RETRY_DELAY_S)  # winner may be mid-write
        return LeaseInfo(pid=None)

    def release(self) -> None:
        """Explicit release for tests/clean shutdown; the OS also releases the
        flock on process exit (the crash path needs no cleanup code)."""
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def read_lease_record(path: Path) -> LeaseInfo | None:
    """Read-only diagnostics for status paths — NEVER takes the flock."""
    try:
        raw = json.loads(path.read_text())
        return LeaseInfo(
            pid=int(raw["pid"]),
            started_at=raw.get("started_at"),
            version=raw.get("version"),
            socket_path=raw.get("socket_path"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
