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


def _resolved_task_bases(
    task: Task,
    config,
    *,
    cli_base: str | None = None,
    base_map: dict[str, str] | None = None,
) -> frozenset[str] | None:
    """Resolve every effective PR base for a task through the shared resolver.

    Reconcile cannot attribute a fetched PR to a repository (#462), so a
    multi-repo task remains in sync when its PR base matches ANY affected
    repo's effective base. Finish-specific CLI inputs apply only to the task
    being finished.
    """
    if not task.affected_repos:
        return frozenset({task.base_branch}) if task.base_branch is not None else None
    repo_configs = config.repos if config is not None else {}
    known_repos = repo_configs.keys() if config is not None else task.affected_repos
    bases: set[str] = set()
    for repo in task.affected_repos:
        resolved = resolve_base(
            repo,
            repo_configs.get(repo),
            cli_base=cli_base,
            base_map=base_map or {},
            known_repos=known_repos,
            task_base=task.base_override,
        )
        effective = resolved if resolved is not None else task.base_branch
        if effective is not None:
            bases.add(effective)
    return frozenset(bases) if bases else None


def resolve_task_bases(
    state: WorkspaceState,
    config,
    *,
    base_inputs_by_slug: dict[
        str, tuple[str | None, dict[str, str]]
    ] | None = None,
) -> dict[str, frozenset[str] | None]:
    """Resolve the effective base set for every task in workspace state."""
    base_inputs_by_slug = base_inputs_by_slug or {}
    resolved: dict[str, frozenset[str] | None] = {}
    for task in state.tasks.values():
        cli_base, base_map = base_inputs_by_slug.get(task.slug, (None, {}))
        resolved[task.slug] = _resolved_task_bases(
            task, config, cli_base=cli_base, base_map=base_map,
        )
    return resolved


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
    base_inputs_by_slug: dict[
        str, tuple[str | None, dict[str, str]]
    ] | None = None,
) -> dict[str, Decision]:
    """Cache-first; fetch on stale; fall back on error. Never raises."""
    resolved_bases = resolve_task_bases(
        state, config, base_inputs_by_slug=base_inputs_by_slug,
    )
    base_context = {
        slug: sorted(bases) if bases is not None else None
        for slug, bases in resolved_bases.items()
    }

    # Keep the raw payload only to preserve its ignore list on a fresh write.
    # Every results-consuming path uses the schema/context-compatible payload.
    payload = cache.read()
    current_payload = cache.current(payload, base_context=base_context)
    if current_payload is not None and cache.is_fresh(current_payload):
        return _decisions_from_cache(state, current_payload)

    branches = [t.branch for t in state.tasks.values()]
    worktrees_by_branch: dict[str, Path] = {}
    for t in state.tasks.values():
        if t.worktrees:
            worktrees_by_branch[t.branch] = next(iter(t.worktrees.values()))

    try:
        pr_by_head, git_by_branch = fetcher(branches, worktrees_by_branch)
    except FetchError:
        if current_payload is not None:
            return _decisions_from_cache(state, current_payload)
        return {}

    tasks_tuples = [
        (t.slug, t.branch, resolved_bases[t.slug]) for t in state.tasks.values()
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
        base_context=base_context,
    ))
    decisions = {slug: _decision_from_detection(slug, d, state) for slug, d in detections.items()}
    return apply_dependency_stale(state, decisions)
