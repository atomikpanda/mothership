"""Pure graph queries over Task.depends_on."""
from __future__ import annotations
from datetime import datetime, timezone

from mship.core.state import Task, WorkspaceState, DependencyEdge
from mship.core.task_graph import (
    CycleError,
    downstream_of,
    find_cycle,
    transitive_upstream,
)


def _now():
    return datetime.now(timezone.utc)


def _task(slug: str, upstream: list[str] = ()) -> Task:
    return Task(
        slug=slug, description=slug, phase="dev",
        created_at=_now(),
        affected_repos=["r"], branch=f"feat/{slug}",
        depends_on=[DependencyEdge(upstream_slug=u, created_at=_now()) for u in upstream],
    )


def _ws(*tasks: Task) -> WorkspaceState:
    return WorkspaceState(tasks={t.slug: t for t in tasks})


def test_transitive_upstream_single_hop():
    ws = _ws(_task("a"), _task("b", ["a"]))
    assert transitive_upstream(ws, "b") == {"a"}


def test_transitive_upstream_multi_hop():
    ws = _ws(_task("a"), _task("b", ["a"]), _task("c", ["b"]))
    assert transitive_upstream(ws, "c") == {"a", "b"}


def test_transitive_upstream_diamond():
    ws = _ws(_task("a"), _task("b", ["a"]), _task("c", ["a"]), _task("d", ["b", "c"]))
    assert transitive_upstream(ws, "d") == {"a", "b", "c"}


def test_transitive_upstream_missing_slug_returns_empty():
    """Unknown task slug returns empty set (caller validates separately)."""
    ws = _ws(_task("a"))
    assert transitive_upstream(ws, "nonexistent") == set()


def test_downstream_of_single_hop():
    ws = _ws(_task("a"), _task("b", ["a"]))
    assert downstream_of(ws, "a") == {"b"}


def test_downstream_of_multi_hop():
    ws = _ws(_task("a"), _task("b", ["a"]), _task("c", ["b"]))
    assert downstream_of(ws, "a") == {"b", "c"}


def test_find_cycle_self_edge():
    """Adding self-edge produces a cycle."""
    ws = _ws(_task("a"))
    # Simulate the would-be edge: would adding a → a create a cycle?
    cycle = find_cycle(ws, downstream="a", new_upstream="a")
    assert cycle == ["a", "a"]


def test_find_cycle_two_node():
    """Adding b→a when a already depends on b creates a cycle."""
    ws = _ws(_task("a", ["b"]), _task("b"))
    cycle = find_cycle(ws, downstream="b", new_upstream="a")
    assert cycle == ["b", "a", "b"]


def test_find_cycle_three_node():
    """Adding c→a when a → b → c creates a cycle."""
    ws = _ws(_task("a", ["b"]), _task("b", ["c"]), _task("c"))
    cycle = find_cycle(ws, downstream="c", new_upstream="a")
    assert cycle == ["c", "a", "b", "c"]


def test_no_cycle_on_diamond():
    """A diamond DAG has no cycle."""
    ws = _ws(_task("a"), _task("b", ["a"]), _task("c", ["a"]))
    assert find_cycle(ws, downstream="d", new_upstream="b") is None
    assert find_cycle(ws, downstream="d", new_upstream="c") is None


def test_cycle_error_carries_path():
    """CycleError preserves the cycle path for error messages."""
    err = CycleError(["a", "b", "a"])
    assert err.path == ["a", "b", "a"]
    assert "a → b → a" in str(err)


def test_is_ready_finished_and_merged():
    """A finished task whose reconcile state is merged is ready."""
    from mship.core.reconcile.detect import UpstreamState
    from mship.core.reconcile.gate import Decision
    from mship.core.task_graph import is_ready

    ws = _ws(_task("a"))
    ws.tasks["a"].finished_at = _now()
    decisions = {
        "a": Decision(slug="a", state=UpstreamState.merged, pr_url=None,
                      pr_number=None, base=None, merge_commit=None,
                      updated_at=None),
    }
    assert is_ready(ws, "a", decisions) is True


def test_is_ready_finished_but_open():
    """A finished task whose PR is still open is NOT ready."""
    from mship.core.reconcile.detect import UpstreamState
    from mship.core.reconcile.gate import Decision
    from mship.core.task_graph import is_ready

    ws = _ws(_task("a"))
    ws.tasks["a"].finished_at = _now()
    decisions = {
        "a": Decision(slug="a", state=UpstreamState.in_sync, pr_url=None,
                      pr_number=None, base=None, merge_commit=None,
                      updated_at=None),
    }
    assert is_ready(ws, "a", decisions) is False


def test_is_ready_unfinished():
    """An unfinished task is never ready."""
    from mship.core.task_graph import is_ready

    ws = _ws(_task("a"))  # finished_at is None
    assert is_ready(ws, "a", {}) is False


def test_is_ready_unknown_task():
    """Unknown slug returns False."""
    from mship.core.task_graph import is_ready

    ws = _ws(_task("a"))
    assert is_ready(ws, "nope", {}) is False
