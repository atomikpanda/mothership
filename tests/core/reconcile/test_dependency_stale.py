"""Tests for dependency_stale post-processing (#104)."""
from __future__ import annotations
from datetime import datetime, timezone

from mship.core.state import DependencyEdge, Task, WorkspaceState
from mship.core.reconcile.detect import UpstreamState
from mship.core.reconcile.gate import Decision
from mship.core.reconcile.dependency_stale import apply_dependency_stale


def _task(slug, created_at, finished_at=None, upstream=()):
    return Task(
        slug=slug, description=slug, phase="dev",
        created_at=created_at,
        finished_at=finished_at,
        affected_repos=["r"], branch=f"feat/{slug}",
        depends_on=[DependencyEdge(upstream_slug=u, created_at=created_at) for u in upstream],
    )


def test_dependency_stale_when_upstream_merged_after_downstream_created():
    """Downstream task → in_sync; upstream merged after downstream created → dependency_stale."""
    t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 10, tzinfo=timezone.utc)
    state = WorkspaceState(tasks={
        "a": _task("a", t0, finished_at=t0),
        "b": _task("b", t0, upstream=["a"]),
    })
    decisions = {
        "a": Decision(slug="a", state=UpstreamState.merged, pr_url=None,
                      pr_number=None, base=None, merge_commit=None,
                      updated_at=t1.isoformat()),
        "b": Decision(slug="b", state=UpstreamState.in_sync, pr_url=None,
                      pr_number=None, base=None, merge_commit=None,
                      updated_at=None),
    }

    out = apply_dependency_stale(state, decisions)
    assert out["b"].state == UpstreamState.dependency_stale


def test_no_override_when_upstream_merged_before_downstream_created():
    """If the upstream merged BEFORE the downstream was created, downstream stays in_sync."""
    t_old = datetime(2026, 5, 1, tzinfo=timezone.utc)
    t_new = datetime(2026, 5, 10, tzinfo=timezone.utc)
    state = WorkspaceState(tasks={
        "a": _task("a", t_old, finished_at=t_old),
        "b": _task("b", t_new, upstream=["a"]),
    })
    decisions = {
        "a": Decision(slug="a", state=UpstreamState.merged, pr_url=None,
                      pr_number=None, base=None, merge_commit=None,
                      updated_at=t_old.isoformat()),
        "b": Decision(slug="b", state=UpstreamState.in_sync, pr_url=None,
                      pr_number=None, base=None, merge_commit=None,
                      updated_at=None),
    }

    out = apply_dependency_stale(state, decisions)
    assert out["b"].state == UpstreamState.in_sync


def test_no_override_when_downstream_already_diverged():
    """If downstream is already in a non-in_sync state, leave it alone."""
    t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 10, tzinfo=timezone.utc)
    state = WorkspaceState(tasks={
        "a": _task("a", t0, finished_at=t0),
        "b": _task("b", t0, upstream=["a"]),
    })
    decisions = {
        "a": Decision(slug="a", state=UpstreamState.merged, pr_url=None,
                      pr_number=None, base=None, merge_commit=None,
                      updated_at=t1.isoformat()),
        "b": Decision(slug="b", state=UpstreamState.diverged, pr_url=None,
                      pr_number=None, base=None, merge_commit=None,
                      updated_at=None),
    }

    out = apply_dependency_stale(state, decisions)
    assert out["b"].state == UpstreamState.diverged
