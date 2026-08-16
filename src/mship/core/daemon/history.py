"""Durable start/stop history + the crash-loop detector.

Daemon-owned so crash-loop visibility behaves identically under systemd,
launchd, and `daemon run` — no NRestarts/`launchctl print` parsing. A start is
UNCLEAN when it is not preceded by a clean stop; only unclean starts count
toward a loop. `run.py` appends the clean-stop entry when uvicorn returns
normally (graceful shutdown).

Atomic 0600 tmp+replace writes (the `run_host/store.py` pattern) are safe here
— nothing flocks this file (the lease file is the one that must never be
replaced).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

MAX_ENTRIES = 20
LOOP_WINDOW_S = 600
LOOP_THRESHOLD = 3


@dataclass(frozen=True)
class HistoryEntry:
    kind: Literal["start", "clean_stop"]
    at: datetime


def read_history(path: Path) -> list[HistoryEntry]:
    try:
        raw = json.loads(path.read_text())
        return [HistoryEntry(kind=e["kind"], at=datetime.fromisoformat(e["at"])) for e in raw]
    except (OSError, ValueError, KeyError, TypeError):
        return []  # missing or corrupt → start fresh


def _append(path: Path, kind: str, now: datetime) -> None:
    entries = read_history(path)
    entries.append(HistoryEntry(kind=kind, at=now))  # type: ignore[arg-type]
    entries = entries[-MAX_ENTRIES:]
    payload = json.dumps([{"kind": e.kind, "at": e.at.isoformat()} for e in entries])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode())
    finally:
        os.close(fd)
    tmp.replace(path)


def append_start(path: Path, now: datetime) -> None:
    _append(path, "start", now)


def append_clean_stop(path: Path, now: datetime) -> None:
    _append(path, "clean_stop", now)


def unclean_start_count(
    entries: list[HistoryEntry], now: datetime, *, window_s: float = LOOP_WINDOW_S
) -> int:
    """Starts inside the window whose immediately preceding entry is not a
    clean stop (crash/kill respawns, not operator restarts)."""
    count = 0
    for i, e in enumerate(entries):
        if e.kind != "start" or (now - e.at).total_seconds() > window_s:
            continue
        if i == 0 or entries[i - 1].kind != "clean_stop":
            count += 1
    return count


def is_crash_looping(
    entries: list[HistoryEntry],
    now: datetime,
    *,
    window_s: float = LOOP_WINDOW_S,
    threshold: int = LOOP_THRESHOLD,
) -> bool:
    return unclean_start_count(entries, now, window_s=window_s) >= threshold
