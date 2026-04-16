"""Reconcile cache: batched gh responses + per-task ignore list."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


CACHE_FILENAME = "reconcile.cache.json"
DEFAULT_TTL_SECONDS = 300


@dataclass
class CachePayload:
    fetched_at: float
    ttl_seconds: int
    results: dict[str, dict]
    ignored: list[str] = field(default_factory=list)


class ReconcileCache:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)
        self._path = self._state_dir / CACHE_FILENAME

    # --- payload ---

    def read(self) -> CachePayload | None:
        if not self._path.is_file():
            return None
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return CachePayload(
                fetched_at=float(data["fetched_at"]),
                ttl_seconds=int(data.get("ttl_seconds", DEFAULT_TTL_SECONDS)),
                results=dict(data.get("results", {})),
                ignored=list(data.get("ignored", [])),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def write(self, payload: CachePayload) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        body = {
            "fetched_at": payload.fetched_at,
            "ttl_seconds": payload.ttl_seconds,
            "results": payload.results,
            "ignored": payload.ignored,
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2))
        tmp.replace(self._path)

    def is_fresh(self, payload: CachePayload) -> bool:
        return (time.time() - payload.fetched_at) < payload.ttl_seconds

    # --- ignore list ---

    def read_ignores(self) -> list[str]:
        payload = self.read()
        return list(payload.ignored) if payload else []

    def add_ignore(self, slug: str) -> None:
        payload = self.read() or CachePayload(
            fetched_at=0.0, ttl_seconds=DEFAULT_TTL_SECONDS, results={}, ignored=[],
        )
        if slug not in payload.ignored:
            payload.ignored.append(slug)
        self.write(payload)

    def remove_ignore(self, slug: str) -> None:
        payload = self.read()
        if payload is None or slug not in payload.ignored:
            return
        payload.ignored = [s for s in payload.ignored if s != slug]
        self.write(payload)

    def clear_ignores(self) -> None:
        payload = self.read()
        if payload is None:
            return
        payload.ignored = []
        self.write(payload)
