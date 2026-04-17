"""Tiny key/value store for workspace-level metadata that doesn't fit in state.yaml.

Currently holds only `last_sync_at` (written by `mship sync`, read by
`mship context`). Kept as a separate JSON file so a corrupt write here can
never wedge `state.yaml`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


META_FILENAME = "workspace_meta.json"


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / META_FILENAME


def _read_raw(state_dir: Path) -> dict:
    p = _path(state_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_raw(state_dir: Path, data: dict) -> None:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    p = _path(state_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(p)


def read_last_sync_at(state_dir: Path) -> Optional[datetime]:
    raw = _read_raw(state_dir).get("last_sync_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def write_last_sync_at(state_dir: Path, when: Optional[datetime] = None) -> None:
    when = when or datetime.now(timezone.utc)
    data = _read_raw(state_dir)
    data["last_sync_at"] = when.isoformat()
    _write_raw(state_dir, data)
