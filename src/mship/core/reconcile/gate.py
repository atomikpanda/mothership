"""Gate: single entry point for `spawn`, `finish`, `close`, pre-commit.

Runs reconcile_now() (cache-first, fetch on stale), then the caller inspects
each Decision via should_block() to choose block/warn/allow per command.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
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


def _decision_from_cache_entry(
    slug: str,
    raw: object,
    state: WorkspaceState,
) -> Decision | None:
    if not isinstance(raw, Mapping):
        return None
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
    except (KeyError, TypeError, ValueError):
        return None


def _decisions_from_cache(
    state: WorkspaceState,
    payload: CachePayload,
    *,
    only_slugs: set[str] | None = None,
) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    slugs = state.tasks if only_slugs is None else only_slugs
    for slug in slugs:
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
    decisions = apply_dependency_stale(state, out)
    if only_slugs is None:
        return decisions
    return {slug: decision for slug, decision in decisions.items() if slug in only_slugs}


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
    only_slugs: set[str] | None = None,
) -> dict[str, Decision]:
    """Cache-first; fetch on stale; fall back on error for selected tasks."""
    resolved_bases = resolve_task_bases(
        state, config, base_inputs_by_slug=base_inputs_by_slug,
    )
    base_context = {
        slug: sorted(bases) if bases is not None else None
        for slug, bases in resolved_bases.items()
    }
    reconciliation_slugs = only_slugs
    if only_slugs is not None:
        reconciliation_slugs = set(only_slugs)
        pending = list(only_slugs)
        while pending:
            task = state.tasks.get(pending.pop())
            if task is None:
                continue
            for edge in task.depends_on:
                if edge.upstream_slug not in reconciliation_slugs:
                    reconciliation_slugs.add(edge.upstream_slug)
                    pending.append(edge.upstream_slug)
    tasks = [
        task for task in state.tasks.values()
        if reconciliation_slugs is None or task.slug in reconciliation_slugs
    ]

    # Keep the raw payload only to preserve its ignore list on a fresh write.
    # Every results-consuming path validates the schema/context-compatible closure.
    payload = cache.read()
    current_payload = cache.current(
        payload,
        base_context=base_context,
        only_slugs=reconciliation_slugs,
    )
    cached_decisions = (
        _decisions_from_cache(
            state, current_payload, only_slugs=reconciliation_slugs,
        )
        if current_payload is not None
        else None
    )
    required_slugs = (
        state.tasks.keys()
        if reconciliation_slugs is None
        else reconciliation_slugs
    )
    cache_complete = (
        cached_decisions is not None
        and required_slugs <= cached_decisions.keys()
    )
    if (
        current_payload is not None
        and cache.is_fresh(current_payload)
        and cache_complete
    ):
        if only_slugs is None:
            return cached_decisions
        return {
            slug: decision
            for slug, decision in cached_decisions.items()
            if slug in only_slugs
        }

    branches = [task.branch for task in tasks]
    worktrees_by_branch: dict[str, Path] = {}
    for task in tasks:
        if task.worktrees:
            worktrees_by_branch[task.branch] = next(iter(task.worktrees.values()))

    try:
        pr_by_head, git_by_branch = fetcher(branches, worktrees_by_branch)
    except FetchError:
        if cached_decisions is not None and cache_complete:
            if only_slugs is None:
                return cached_decisions
            return {
                slug: decision
                for slug, decision in cached_decisions.items()
                if slug in only_slugs
            }
        return {}

    tasks_tuples = [
        (task.slug, task.branch, resolved_bases[task.slug])
        for task in tasks
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
    if only_slugs is None:
        cache.write(CachePayload(
            fetched_at=time.time(),
            ttl_seconds=ttl_seconds,
            results=results,
            ignored=(payload.ignored if payload else []),
            base_context=base_context,
        ))
    decisions = {
        slug: _decision_from_detection(slug, d, state)
        for slug, d in detections.items()
    }
    if cached_decisions is not None and only_slugs is not None:
        cached_decisions.update(decisions)
        decisions = cached_decisions
    decisions = apply_dependency_stale(state, decisions)
    if only_slugs is None:
        return decisions
    return {slug: decision for slug, decision in decisions.items() if slug in only_slugs}
