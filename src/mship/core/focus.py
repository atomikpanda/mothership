"""Persisted CURRENT FOCUS for the v2 cockpit (spec cockpit-v2, ac1).

A tiny per-workspace file holding the focused WorkItem id + a monotonic
timestamp. Pure over a Path — no container, no Textual — so it is unit-testable
directly. `mship layout focus` writes it; `mship view … --follow` reads it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FOCUS_FILENAME = "cockpit-focus.json"


@dataclass(frozen=True)
class FocusState:
    work_item_id: str
    updated_at: datetime


def focus_path(state_dir) -> Path:
    """The focus file inside a workspace's state dir."""
    return Path(state_dir) / FOCUS_FILENAME


def write_focus(path: Path, work_item_id: str, *, now: datetime | None = None) -> FocusState:
    """Record `work_item_id` as focused with a monotonic UTC timestamp; returns the
    written state. Creates parent dirs so the first focus in a fresh workspace works."""
    ts = now if now is not None else datetime.now(timezone.utc)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"work_item_id": work_item_id, "updated_at": ts.isoformat()}) + "\n"
    )
    return FocusState(work_item_id=work_item_id, updated_at=ts)


def read_focus(path: Path) -> FocusState | None:
    """Read the focus state, or None when the file is absent/unreadable/malformed —
    a missing or corrupt focus file must degrade to 'no focus', never raise."""
    try:
        raw = Path(path).read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
        work_item_id = data["work_item_id"]
        updated_at = datetime.fromisoformat(data["updated_at"])
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(work_item_id, str) or not work_item_id:
        return None
    return FocusState(work_item_id=work_item_id, updated_at=updated_at)
