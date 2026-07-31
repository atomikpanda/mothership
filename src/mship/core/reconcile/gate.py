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

from mship.core.state import WorkspaceState, Task
from mship.core.base_resolver import resolve_base
from mship.core.reconcile.cache import ReconcileCache, CachePayload, DEFAULT_TTL_SECONDS
from mship.core.reconcile.detect import (
    Detection, GitSnapshot, PRSnapshot, UpstreamState, detect_many,
)
from mship.core.reconcile.dependency_stale import apply_dependency_stale
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
    finished_at: str | None = None


def _finished_at_for(slug: str, state: WorkspaceState) -> str | None:
    """Return the ISO-8601 string of the task's finished_at, or None.

    Used at Decision-construction time to propagate finish-state into the
    gate's settled-task auto-allow path (issue #36).
    """
    task = state.tasks.get(slug)
    if task is None or task.finished_at is None:
        return None
    return task.finished_at.isoformat()


class GateAction(str, Enum):
    allow = "allow"
    warn = "warn"
    block = "block"


_MATRIX: dict[str, dict[str, GateAction]] = {
    "in_sync":           {"spawn": GateAction.allow, "finish": GateAction.allow, "close": GateAction.allow, "precommit": GateAction.allow},
    "merged":            {"spawn": GateAction.block, "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.block},
    "closed":            {"spawn": GateAction.block, "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.block},
    "diverged":          {"spawn": GateAction.warn,  "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.block},
    "base_changed":      {"spawn": GateAction.warn,  "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.allow},
    "missing":           {"spawn": GateAction.allow, "finish": GateAction.allow, "close": GateAction.allow, "precommit": GateAction.allow},
    "dependency_stale":  {"spawn": GateAction.warn,  "finish": GateAction.block, "close": GateAction.allow, "precommit": GateAction.warn},
}


def should_block(decision: Decision, *, command: Command, ignored: list[str]) -> GateAction:
    if decision.slug in ignored:
        return GateAction.allow
    # Settled: a task whose PR is merged/closed AND whose finished_at is set.
    # The user has already run `mship finish`; only `mship close` remains.
    # Don't block subsequent `spawn`/`finish` on these tasks — surface them
    # via `mship reconcile` (existing output) instead. Issue #36.
    if (
        decision.finished_at is not None
        and decision.state in (UpstreamState.merged, UpstreamState.closed)
        and command in ("spawn", "finish")
    ):
        return GateAction.allow
    return _MATRIX[decision.state.value][command]


def _resolved_task_bases(task: Task, config) -> frozenset[str] | None:
    """The set of bases finish/PR-creation could target for this task (#455,
    extended per-repo in #461).

    `task.base_branch` is the spawn-time recorded default (usually the
    workspace default, e.g. "main") — NOT necessarily where `finish` opens
    the PR. `finish` resolves the real target via
    `mship.core.base_resolver.resolve_base` (base_map > cli_base >
    base_override > repo_config.base_branch); reconcile has no CLI overrides
    in play, so only `base_override` and the repo's configured base apply
    here — same precedence, same resolver, no duplicated logic (mirrors
    `mship.core.context._effective_base_for_repo`).

    A multi-repo task's repos can each have a DIFFERENT configured
    `repos.<name>.base_branch`. `reconcile_now` matches PR snapshots by
    branch name only — it has no way to tell which of the task's repos a
    given PR came from — so instead of resolving a single base from one
    arbitrarily chosen repo (which false-flags `base_changed` whenever the
    matched PR actually belongs to a *different* affected repo), this
    resolves a base PER repo and returns the whole set. A PR is in sync if
    its base matches ANY of them.

    Falls back to `{task.base_branch}` when there's no config or no repos to
    resolve against, preserving prior behavior for config-less workspaces.
    """
    if config is None or not task.affected_repos:
        return frozenset({task.base_branch}) if task.base_branch is not None else None
    bases: set[str] = set()
    for repo in task.affected_repos:
        resolved = resolve_base(
            repo, config.repos.get(repo), cli_base=None, base_map={},
            known_repos=config.repos.keys(), task_base=task.base_override,
        )
        effective = resolved if resolved is not None else task.base_branch
        if effective is not None:
            bases.add(effective)
    return frozenset(bases) if bases else None


Fetcher = Callable[[list[str], dict[str, Path]], tuple[dict[str, PRSnapshot], dict[str, GitSnapshot]]]


def _decision_from_detection(slug: str, det: Detection, state: WorkspaceState) -> Decision:
    return Decision(
        slug=slug, state=det.state, pr_url=det.pr_url, pr_number=det.pr_number,
        base=det.base, merge_commit=det.merge_commit, updated_at=det.updated_at,
        finished_at=_finished_at_for(slug, state),
    )


def _decision_from_cache_entry(slug: str, raw: dict, state: WorkspaceState) -> Decision | None:
    try:
        return Decision(
            slug=slug,
            state=UpstreamState(raw["state"]),
            pr_url=raw.get("pr_url"),
            pr_number=raw.get("pr_number"),
            base=raw.get("base"),
            merge_commit=raw.get("merge_commit"),
            updated_at=raw.get("updated_at"),
            finished_at=_finished_at_for(slug, state),
        )
    except (KeyError, ValueError):
        return None


def _decisions_from_cache(state: WorkspaceState, payload: CachePayload) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    for slug in state.tasks:
        raw = payload.results.get(slug)
        if raw is None:
            continue
        d = _decision_from_cache_entry(slug, raw, state)
        if d is not None:
            out[slug] = d
    # Apply the dependency-stale override here too (#104). It's derived from LIVE task state
    # (depends_on edges + the upstream's cached merge state), not from a fresh fetch, so it must be
    # recomputed on every path. The fresh-fetch path applies it at the bottom of reconcile_now; both
    # cache paths (fresh-cache hit + FetchError fallback) route through here. Without this a warm
    # cache — the common case, since reconcile shares one 300s cache across spawn/finish/close/
    # precommit — silently drops it and a dependency-stale task reverts to in_sync (finish stops
    # blocking on the stale base).
    return apply_dependency_stale(state, out)


def reconcile_now(
    state: WorkspaceState,
    *,
    cache: ReconcileCache,
    fetcher: Fetcher,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    config=None,
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

    tasks_tuples = [
        (t.slug, t.branch, _resolved_task_bases(t, config)) for t in state.tasks.values()
    ]
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
    decisions = {slug: _decision_from_detection(slug, d, state) for slug, d in detections.items()}
    return apply_dependency_stale(state, decisions)
