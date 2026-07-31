"""Reconcile cache: batched gh responses + per-task ignore list."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


CACHE_FILENAME = "reconcile.cache.json"
DEFAULT_TTL_SECONDS = 300

# Bump whenever the *logic that produces `results`* changes (e.g. #461's
# per-repo base resolution) so an already-cached, TTL-fresh entry computed
# under the old logic is treated as a miss and recomputed, instead of being
# served stale until the TTL lapses or a manual refresh (#461 follow-up).
SCHEMA_VERSION = 2


@dataclass
class CachePayload:
    fetched_at: float
    ttl_seconds: int
    results: dict[str, dict]
    ignored: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION


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
                # Entries written before this field existed have no "schema_version"
                # key; default to 0 so they never collide with a real SCHEMA_VERSION
                # and are correctly treated as stale by is_fresh().
                schema_version=int(data.get("schema_version", 0)),
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
            # Preserve the payload's own schema_version rather than always stamping
            # the current constant. Freshly-computed results are built with a bare
            # CachePayload(...), which defaults schema_version to the current
            # SCHEMA_VERSION, so they still cache correctly. But the ignore-list
            # mutators (add_ignore/remove_ignore/clear_ignores) do a
            # read-modify-write that preserves an existing entry's old `results`
            # and `fetched_at` — if that write stamped the current version anyway,
            # a TTL-fresh pre-v2 entry (with a stale, spuriously-computed result)
            # would be laundered into looking current-version-fresh, bypassing
            # the schema_version staleness gate (#461 follow-up).
            "schema_version": payload.schema_version,
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2))
        tmp.replace(self._path)

    def is_fresh(self, payload: CachePayload) -> bool:
        if payload.schema_version != SCHEMA_VERSION:
            return False
        return (time.time() - payload.fetched_at) < payload.ttl_seconds

    def current(self, payload: CachePayload | None) -> CachePayload | None:
        """The single load-boundary schema gate: treat a schema-mismatched
        entry as absent (a cache miss) for every results-consuming reader.

        Callers must run a `read()` result through this once, immediately
        after loading it, and branch on ITS return value from then on — not
        re-inspect the raw payload. Without this, each results-consuming path
        has to remember its own schema check; `reconcile_now`'s fetch-error
        fallback forgot to (#461 follow-up, was P1 "Invalid cache survives
        fallback", cache.py:82) and served a pre-v2 spurious base_changed on
        a live-fetch failure. Gating once, at load, makes that class of bug
        structurally impossible: a schema-invalid entry never reaches any
        downstream reader to be forgotten about in the first place.

        Deliberately schema-only, not TTL — the fetch-error fallback serves a
        TTL-stale-but-schema-valid entry on purpose (better than nothing);
        only `is_fresh()` enforces TTL for the normal path.
        """
        if payload is None or payload.schema_version != SCHEMA_VERSION:
            return None
        return payload

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
