"""Pure graph queries over Task.depends_on edges.

This module is distinct from `mship.core.graph` (which models repo
dependency topology). The task graph operates on `WorkspaceState.tasks`.
"""
from __future__ import annotations

from mship.core.state import WorkspaceState


class CycleError(Exception):
    """Adding a proposed edge would create a cycle."""

    def __init__(self, path: list[str]) -> None:
        self.path = path
        super().__init__(" → ".join(path))


def transitive_upstream(state: WorkspaceState, slug: str) -> set[str]:
    """Return all transitive upstream slugs of `slug`, excluding `slug` itself."""
    if slug not in state.tasks:
        return set()
    visited: set[str] = set()
    stack = [slug]
    while stack:
        node = stack.pop()
        task = state.tasks.get(node)
        if task is None:
            continue
        for edge in task.depends_on:
            if edge.upstream_slug not in visited:
                visited.add(edge.upstream_slug)
                stack.append(edge.upstream_slug)
    return visited


def downstream_of(state: WorkspaceState, slug: str) -> set[str]:
    """Return all transitive downstream slugs of `slug`, excluding `slug` itself."""
    direct: dict[str, list[str]] = {s: [] for s in state.tasks}
    for s, t in state.tasks.items():
        for edge in t.depends_on:
            if edge.upstream_slug in direct:
                direct[edge.upstream_slug].append(s)

    visited: set[str] = set()
    stack = list(direct.get(slug, []))
    while stack:
        node = stack.pop()
        if node not in visited:
            visited.add(node)
            stack.extend(direct.get(node, []))
    return visited


def find_cycle(
    state: WorkspaceState,
    *,
    downstream: str,
    new_upstream: str,
) -> list[str] | None:
    """If adding edge (downstream → new_upstream) creates a cycle, return the cycle path.

    The path is [downstream, new_upstream, ..., downstream] — first and last
    are the same slug.

    Returns None if no cycle would be created.

    Note: `downstream` need not exist in `state.tasks` yet (we use this at
    spawn time, before the task is persisted).
    """
    if new_upstream == downstream:
        return [downstream, downstream]

    if new_upstream not in state.tasks:
        return None

    parent: dict[str, str] = {new_upstream: downstream}
    stack = [new_upstream]
    while stack:
        node = stack.pop()
        task = state.tasks.get(node)
        if task is None:
            continue
        for edge in task.depends_on:
            up = edge.upstream_slug
            if up == downstream:
                # Found cycle: reconstruct path from downstream to node
                path = [node]
                current = node
                while current != new_upstream:
                    current = parent[current]
                    path.append(current)
                path.reverse()
                # path is now [downstream, ..., node]
                # return [downstream, ..., node (=new_upstream), downstream]
                return [downstream] + path + [downstream]
            if up not in parent:
                parent[up] = node
                stack.append(up)
    return None


def is_ready(
    state: WorkspaceState,
    slug: str,
    reconcile_decisions: dict,
) -> bool:
    """True iff `slug` is a finished task whose reconcile state is merged.

    `reconcile_decisions` is a `dict[str, Decision]` from
    `mship.core.reconcile.gate.reconcile_now`. We import lazily to avoid a
    circular import — reconcile may grow dependency-aware logic later.
    """
    from mship.core.reconcile.detect import UpstreamState

    task = state.tasks.get(slug)
    if task is None or task.finished_at is None:
        return False
    decision = reconcile_decisions.get(slug)
    if decision is None:
        return False
    return decision.state == UpstreamState.merged
