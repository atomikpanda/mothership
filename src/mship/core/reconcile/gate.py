"""Gate: single entry point for `spawn`, `finish`, `close`, pre-commit.

Runs reconcile_now() (cache-first, fetch on stale), then the caller inspects
each Decision via should_block() to choose block/warn/allow per command.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Literal

from mship.core.state import WorkspaceState
from mship.core.reconcile.cache import ReconcileCache, CachePayload, DEFAULT_TTL_SECONDS
from mship.core.reconcile.detect import (
    Detection, GitSnapshot, PRSnapshot, UpstreamState, detect_many,
)
from mship.core.reconcile.fetch import FetchError


Command = Literal["spawn", "finish", "close", "precommit"]


@dataclass(frozen=True)
class Decision:
    slug: str
    state: UpstreamState
    pr_url: str | None
    pr_number: int | None
    base: str | None
    merge_commit: str | None
    updated_at: str | None


class GateAction(str, Enum):
    allow = "allow"
    warn = "warn"
    block = "block"


_MATRIX: dict[str, dict[str, GateAction]] = {
    "in_sync":      {"spawn": GateAction.allow, "finish": GateAction.allow, "close": GateAction.allow, "precommit": GateAction.allow},
    "merged":       {"spawn": GateAction.block, "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.block},
    "closed":       {"spawn": GateAction.block, "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.block},
    "diverged":     {"spawn": GateAction.warn,  "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.block},
    "base_changed": {"spawn": GateAction.warn,  "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.allow},
    "missing":      {"spawn": GateAction.allow, "finish": GateAction.allow, "close": GateAction.allow, "precommit": GateAction.allow},
}


def should_block(decision: Decision, *, command: Command, ignored: list[str]) -> GateAction:
    if decision.slug in ignored:
        return GateAction.allow
    return _MATRIX[decision.state.value][command]


Fetcher = Callable[[list[str], dict[str, Path]], tuple[dict[str, PRSnapshot], dict[str, GitSnapshot]]]


def _decision_from_detection(slug: str, det: Detection) -> Decision:
    return Decision(
        slug=slug, state=det.state, pr_url=det.pr_url, pr_number=det.pr_number,
        base=det.base, merge_commit=det.merge_commit, updated_at=det.updated_at,
    )


def _decision_from_cache_entry(slug: str, raw: dict) -> Decision | None:
    try:
        return Decision(
            slug=slug,
            state=UpstreamState(raw["state"]),
            pr_url=raw.get("pr_url"),
            pr_number=raw.get("pr_number"),
            base=raw.get("base"),
            merge_commit=raw.get("merge_commit"),
            updated_at=raw.get("updated_at"),
        )
    except (KeyError, ValueError):
        return None


def _decisions_from_cache(state: WorkspaceState, payload: CachePayload) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    for slug in state.tasks:
        raw = payload.results.get(slug)
        if raw is None:
            continue
        d = _decision_from_cache_entry(slug, raw)
        if d is not None:
            out[slug] = d
    return out


def reconcile_now(
    state: WorkspaceState,
    *,
    cache: ReconcileCache,
    fetcher: Fetcher,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Decision]:
    """Cache-first; fetch on stale; fall back on error. Never raises."""
    payload = cache.read()
    if payload and cache.is_fresh(payload):
        return _decisions_from_cache(state, payload)

    branches = [t.branch for t in state.tasks.values()]
    worktrees_by_branch: dict[str, Path] = {}
    for t in state.tasks.values():
        if t.worktrees:
            worktrees_by_branch[t.branch] = next(iter(t.worktrees.values()))

    try:
        pr_by_head, git_by_branch = fetcher(branches, worktrees_by_branch)
    except FetchError:
        if payload is not None:
            return _decisions_from_cache(state, payload)
        return {}

    tasks_tuples = [(t.slug, t.branch, t.base_branch) for t in state.tasks.values()]
    detections = detect_many(tasks_tuples, pr_by_head, git_by_branch)

    results = {
        slug: {
            "state": d.state.value,
            "pr_url": d.pr_url, "pr_number": d.pr_number,
            "base": d.base, "merge_commit": d.merge_commit,
            "updated_at": d.updated_at,
        }
        for slug, d in detections.items()
    }
    cache.write(CachePayload(
        fetched_at=time.time(),
        ttl_seconds=ttl_seconds,
        results=results,
        ignored=(payload.ignored if payload else []),
    ))
    return {slug: _decision_from_detection(slug, d) for slug, d in detections.items()}
