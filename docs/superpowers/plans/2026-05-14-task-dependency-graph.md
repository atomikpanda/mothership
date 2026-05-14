# Task dependency graph — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class `depends_on` edges between tasks, persisted in `state.yaml`. An edge is a constraint: `finish` refuses until every upstream is merged. `status`, `dispatch`, `reconcile` surface the graph.

**Architecture:** Add a single `DependencyEdge` Pydantic model and a `depends_on: list[DependencyEdge]` field on `Task` (additive; default `[]`). All graph queries (cycle detection, transitive upstream, downstream-of, readiness) live in a new pure module `src/mship/core/task_graph.py` — separate from the existing `graph.py` (which models **repo** dependencies, not task dependencies). New CLI verb group `mship depends` (add/remove/list). Existing commands (`spawn`, `finish`, `close`, `status`, `dispatch`, `reconcile`) grow narrow integration hooks.

**Tech Stack:** Python 3, Typer, Pydantic, pytest. No new dependencies.

**Divergence from spec:** spec said "Modified: `src/mship/core/graph.py`". `graph.py` already exists for **repo** dependency topology. Mixing two semantically-different graph types in one module is confusing. The new code lives in `src/mship/core/task_graph.py` instead. Spec updated to match if approved.

---

## Spec reference

`docs/superpowers/specs/2026-05-14-task-dependency-graph-design.md` in this worktree.

## File structure

**New:**

- `src/mship/core/task_graph.py` — pure graph functions: `transitive_upstream`, `downstream_of`, `find_cycle`, `is_ready`. No I/O. Takes `WorkspaceState` as input.
- `src/mship/cli/depends.py` — `mship depends add/remove/list` Typer subcommand group.
- `tests/core/test_task_graph.py` — unit tests for graph functions.
- `tests/cli/test_depends.py` — CLI tests for the `depends` verb group.
- `tests/test_dependency_integration.py` — one end-to-end test exercising spawn → finish-blocked → finish-upstream → finish-downstream.

**Modified:**

- `src/mship/core/state.py` — add `DependencyEdge`; add `depends_on: list[DependencyEdge] = []` field on `Task`.
- `src/mship/cli/__init__.py` — register the `depends` subcommand group.
- `src/mship/cli/worktree.py` — `spawn --depends-on`; `finish` blocked-by-deps check + `--bypass-deps`; `close` downstream check + `--cascade` / `--detach-downstream`.
- `src/mship/cli/status.py` — emit `dependencies` block under `resolved_task`.
- `src/mship/core/dispatch.py` — add `## Dependencies` section to the prompt template.
- `src/mship/core/reconcile/detect.py` — extend `UpstreamState` with `dependency_stale`; post-process detections to apply it.
- `src/mship/cli/reconcile.py` — add `dependency_stale` to action hints and glyph mapping; ensure JSON output passes it through.
- `tests/cli/test_status.py` — assert `dependencies` block.
- `tests/cli/test_worktree.py` (or the equivalent for spawn/finish/close) — extend existing spawn/finish/close tests.
- `tests/cli/test_dispatch.py` — assert `## Dependencies` section.
- `tests/core/reconcile/...` — extend reconcile detection tests for `dependency_stale`.
- `src/mship/skills/working-with-mothership/SKILL.md` — document the new verb and `--depends-on` flag.
- `README.md`, `AGENTS.md`, `GEMINI.md` — add `mship depends` to the cheat sheet; mention `--depends-on`/`--bypass-deps` flags.

**Not modified:**

- `src/mship/core/graph.py` — repo dependency graph; unrelated to task dependencies.

---

## Task 1: Add `DependencyEdge` model and `Task.depends_on` field

**Files:**
- Modify: `src/mship/core/state.py`
- Modify: `tests/core/test_state.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_state.py`:

```python
def test_task_depends_on_defaults_empty(tmp_path):
    """New Task model has depends_on field defaulting to []."""
    from mship.core.state import Task
    from datetime import datetime, timezone

    t = Task(
        slug="x", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], branch="feat/x",
    )
    assert t.depends_on == []


def test_dependency_edge_roundtrip(tmp_path):
    """DependencyEdge serializes and deserializes through state.yaml."""
    from mship.core.state import StateManager, Task, WorkspaceState, DependencyEdge
    from datetime import datetime, timezone

    sm = StateManager(tmp_path / ".mothership")
    now = datetime.now(timezone.utc)
    t = Task(
        slug="b", description="d", phase="dev",
        created_at=now,
        affected_repos=["r"], branch="feat/b",
        depends_on=[DependencyEdge(upstream_slug="a", created_at=now)],
    )
    sm.save(WorkspaceState(tasks={"b": t}))

    loaded = sm.load()
    assert "b" in loaded.tasks
    edges = loaded.tasks["b"].depends_on
    assert len(edges) == 1
    assert edges[0].upstream_slug == "a"


def test_legacy_state_without_depends_on_loads_clean(tmp_path):
    """state.yaml without depends_on field loads with depends_on=[]."""
    from mship.core.state import StateManager
    import yaml

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    (state_dir / "state.yaml").write_text(yaml.dump({
        "tasks": {
            "t": {
                "slug": "t", "description": "d", "phase": "dev",
                "created_at": "2026-05-14T00:00:00+00:00",
                "affected_repos": ["r"], "worktrees": {},
                "branch": "feat/t",
            }
        }
    }))
    sm = StateManager(state_dir)
    state = sm.load()
    assert state.tasks["t"].depends_on == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/core/test_state.py::test_task_depends_on_defaults_empty \
       tests/core/test_state.py::test_dependency_edge_roundtrip \
       tests/core/test_state.py::test_legacy_state_without_depends_on_loads_clean -v
```

Expected: FAIL with `AttributeError: depends_on` or `ImportError: DependencyEdge`.

- [ ] **Step 3: Add the model and field**

In `src/mship/core/state.py`, add after `TestResult`:

```python
class DependencyEdge(BaseModel):
    upstream_slug: str
    created_at: datetime
```

In the `Task` class, add the field (e.g., near `passive_repos`):

```python
    depends_on: list[DependencyEdge] = []
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/core/test_state.py -v
```

Expected: all three new tests PASS; all previously-passing tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/state.py tests/core/test_state.py
git commit -m "feat(state): add DependencyEdge and Task.depends_on field (#104)"
mship journal "state: added DependencyEdge + Task.depends_on; tests passing" --action committed
```

---

## Task 2: Pure graph queries — `task_graph.py`

**Files:**
- Create: `src/mship/core/task_graph.py`
- Create: `tests/core/test_task_graph.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_task_graph.py`:

```python
"""Pure graph queries over Task.depends_on."""
from __future__ import annotations
from datetime import datetime, timezone

import pytest

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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/core/test_task_graph.py -v
```

Expected: FAIL with `ModuleNotFoundError: mship.core.task_graph`.

- [ ] **Step 3: Implement `task_graph.py`**

Create `src/mship/core/task_graph.py`:

```python
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
                path = [downstream, node]
                while path[-1] != new_upstream:
                    path.append(parent[path[-1]])
                path.append(new_upstream)
                path.reverse()
                path.insert(0, downstream)
                return path
            if up not in parent:
                parent[up] = node
                stack.append(up)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/core/test_task_graph.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/task_graph.py tests/core/test_task_graph.py
git commit -m "feat(task_graph): cycle detection + transitive upstream/downstream queries (#104)"
mship journal "task_graph module added; cycle + transitive queries tested" --action committed
```

---

## Task 3: Readiness query — `is_ready`

**Files:**
- Modify: `src/mship/core/task_graph.py`
- Modify: `tests/core/test_task_graph.py`

`is_ready(state, slug, reconcile_decisions)` consumes the same `Decision` dict that `mship.core.reconcile.gate.reconcile_now` produces. Ready ⇔ task has `finished_at` set AND reconcile reports `UpstreamState.merged`. A task with no upstream is trivially-ready for blocking purposes (this function answers "is this task ready to satisfy a downstream's dependency on it").

- [ ] **Step 1: Add the failing test**

Append to `tests/core/test_task_graph.py`:

```python
def test_is_ready_finished_and_merged():
    """A finished task whose reconcile state is merged is ready."""
    from datetime import datetime, timezone
    from mship.core.reconcile.detect import UpstreamState
    from mship.core.reconcile.gate import Decision
    from mship.core.task_graph import is_ready

    ws = _ws(_task("a"))
    ws.tasks["a"].finished_at = datetime.now(timezone.utc)
    decisions = {
        "a": Decision(slug="a", state=UpstreamState.merged, pr_url=None,
                      pr_number=None, base=None, merge_commit=None),
    }
    assert is_ready(ws, "a", decisions) is True


def test_is_ready_finished_but_open():
    """A finished task whose PR is still open is NOT ready."""
    from datetime import datetime, timezone
    from mship.core.reconcile.detect import UpstreamState
    from mship.core.reconcile.gate import Decision
    from mship.core.task_graph import is_ready

    ws = _ws(_task("a"))
    ws.tasks["a"].finished_at = datetime.now(timezone.utc)
    decisions = {
        "a": Decision(slug="a", state=UpstreamState.in_sync, pr_url=None,
                      pr_number=None, base=None, merge_commit=None),
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
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/core/test_task_graph.py -k is_ready -v
```

Expected: FAIL with `ImportError: cannot import name 'is_ready'`.

- [ ] **Step 3: Add `is_ready` to `task_graph.py`**

Append to `src/mship/core/task_graph.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/core/test_task_graph.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/task_graph.py tests/core/test_task_graph.py
git commit -m "feat(task_graph): is_ready query against reconcile decisions (#104)"
mship journal "is_ready query landed; consumes reconcile.gate Decision dict" --action committed
```

---

## Task 4: `mship depends add` command

**Files:**
- Create: `src/mship/cli/depends.py`
- Modify: `src/mship/cli/__init__.py`
- Create: `tests/cli/test_depends.py`

- [ ] **Step 1: Write the failing test**

Create `tests/cli/test_depends.py`:

```python
"""Tests for the `mship depends` verb group."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


def _seed(workspace: Path, *tasks: Task) -> None:
    sm = StateManager(workspace / ".mothership")
    sm.save(WorkspaceState(tasks={t.slug: t for t in tasks}))


def _task(slug: str) -> Task:
    return Task(
        slug=slug, description=slug, phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"], branch=f"feat/{slug}",
    )


@pytest.fixture
def configured_app(workspace: Path):
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    (workspace / ".mothership").mkdir(exist_ok=True)
    yield
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_depends_add_creates_edge(workspace, configured_app):
    _seed(workspace, _task("a"), _task("b"))
    result = runner.invoke(app, ["depends", "add", "a", "--task", "b"])
    assert result.exit_code == 0, result.stderr or result.output

    sm = StateManager(workspace / ".mothership")
    state = sm.load()
    edges = state.tasks["b"].depends_on
    assert len(edges) == 1
    assert edges[0].upstream_slug == "a"


def test_depends_add_unknown_upstream_errors(workspace, configured_app):
    _seed(workspace, _task("b"))
    result = runner.invoke(app, ["depends", "add", "nope", "--task", "b"])
    assert result.exit_code != 0
    assert "nope" in (result.stderr or result.output).lower()


def test_depends_add_self_edge_rejected(workspace, configured_app):
    _seed(workspace, _task("b"))
    result = runner.invoke(app, ["depends", "add", "b", "--task", "b"])
    assert result.exit_code != 0
    err = (result.stderr or result.output).lower()
    assert "cycle" in err or "self" in err


def test_depends_add_cycle_rejected(workspace, configured_app):
    """b already depends on a; adding a→b creates a cycle."""
    a = _task("a")
    b = _task("b")
    from mship.core.state import DependencyEdge
    b.depends_on = [DependencyEdge(upstream_slug="a", created_at=datetime.now(timezone.utc))]
    _seed(workspace, a, b)

    result = runner.invoke(app, ["depends", "add", "b", "--task", "a"])
    assert result.exit_code != 0
    err = (result.stderr or result.output).lower()
    assert "cycle" in err
    assert "b" in err and "a" in err


def test_depends_add_duplicate_idempotent(workspace, configured_app):
    """Adding an existing edge is a no-op (does not duplicate)."""
    _seed(workspace, _task("a"), _task("b"))
    runner.invoke(app, ["depends", "add", "a", "--task", "b"])
    result = runner.invoke(app, ["depends", "add", "a", "--task", "b"])
    assert result.exit_code == 0
    sm = StateManager(workspace / ".mothership")
    edges = sm.load().tasks["b"].depends_on
    assert len(edges) == 1
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/cli/test_depends.py -v
```

Expected: FAIL — `depends` command doesn't exist yet.

- [ ] **Step 3: Implement `depends.py` and register**

Create `src/mship/cli/depends.py`:

```python
"""`mship depends` — manage task-to-task dependency edges (#104)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output
from mship.core.state import DependencyEdge
from mship.core.task_graph import find_cycle


def register(app: typer.Typer, get_container):
    depends_app = typer.Typer(no_args_is_help=True, help="Manage task-to-task dependencies (#104).")
    app.add_typer(depends_app, name="depends")

    @depends_app.command("add")
    def add(
        upstream_slug: str = typer.Argument(..., help="Upstream task slug to depend on."),
        task: Optional[str] = typer.Option(None, "--task", help="Downstream task (defaults to cwd-resolved)."),
    ):
        """Declare that the current/specified task depends on <upstream_slug>."""
        output = Output()
        container = get_container()
        state_mgr = container.state_manager()
        state = state_mgr.load()
        resolved = resolve_for_command("depends", state, task, output)
        downstream = resolved.task.slug

        if upstream_slug not in state.tasks:
            known = ", ".join(sorted(state.tasks.keys())) or "(none)"
            output.error(f"Unknown upstream task: {upstream_slug!r}. Known: {known}.")
            raise typer.Exit(code=1)

        cycle = find_cycle(state, downstream=downstream, new_upstream=upstream_slug)
        if cycle is not None:
            output.error(f"Cycle detected: {' → '.join(cycle)}")
            raise typer.Exit(code=1)

        def _mutate(s):
            t = s.tasks[downstream]
            if any(e.upstream_slug == upstream_slug for e in t.depends_on):
                return  # idempotent
            t.depends_on.append(
                DependencyEdge(upstream_slug=upstream_slug, created_at=datetime.now(timezone.utc))
            )

        state_mgr.mutate(_mutate)
        if output.is_tty:
            output.success(f"{downstream} now depends on {upstream_slug}")
        else:
            output.json({"downstream": downstream, "upstream": upstream_slug, "added": True})
```

In `src/mship/cli/__init__.py`, add the import (alphabetical/grouped near other imports):

```python
from mship.cli import depends as _depends_mod
```

And the registration (alphabetical/grouped near other registrations):

```python
_depends_mod.register(app, get_container)
```

- [ ] **Step 4: Run to verify pass**

```bash
pytest tests/cli/test_depends.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/depends.py src/mship/cli/__init__.py tests/cli/test_depends.py
git commit -m "feat(cli): mship depends add with cycle detection (#104)"
mship journal "mship depends add command landed; cycle check + idempotent add" --action committed
```

---

## Task 5: `mship depends remove` command

**Files:**
- Modify: `src/mship/cli/depends.py`
- Modify: `tests/cli/test_depends.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/cli/test_depends.py`:

```python
def test_depends_remove_clears_edge(workspace, configured_app):
    from mship.core.state import DependencyEdge
    a = _task("a")
    b = _task("b")
    b.depends_on = [DependencyEdge(upstream_slug="a", created_at=datetime.now(timezone.utc))]
    _seed(workspace, a, b)

    result = runner.invoke(app, ["depends", "remove", "a", "--task", "b"])
    assert result.exit_code == 0
    sm = StateManager(workspace / ".mothership")
    assert sm.load().tasks["b"].depends_on == []


def test_depends_remove_missing_edge_errors(workspace, configured_app):
    """Removing an edge that doesn't exist errors loudly."""
    _seed(workspace, _task("a"), _task("b"))
    result = runner.invoke(app, ["depends", "remove", "a", "--task", "b"])
    assert result.exit_code != 0
    err = (result.stderr or result.output).lower()
    assert "no edge" in err or "not found" in err
```

- [ ] **Step 2: Verify failure**

```bash
pytest tests/cli/test_depends.py -k remove -v
```

Expected: FAIL — `remove` subcommand doesn't exist.

- [ ] **Step 3: Add the `remove` command**

In `src/mship/cli/depends.py`, inside the `register` function after `add`, append:

```python
    @depends_app.command("remove")
    def remove(
        upstream_slug: str = typer.Argument(..., help="Upstream task slug to detach from."),
        task: Optional[str] = typer.Option(None, "--task", help="Downstream task (defaults to cwd-resolved)."),
    ):
        """Remove the dependency edge from the current/specified task to <upstream_slug>."""
        output = Output()
        container = get_container()
        state_mgr = container.state_manager()
        state = state_mgr.load()
        resolved = resolve_for_command("depends", state, task, output)
        downstream = resolved.task.slug

        t = state.tasks[downstream]
        if not any(e.upstream_slug == upstream_slug for e in t.depends_on):
            output.error(f"No edge from {downstream!r} to {upstream_slug!r}.")
            raise typer.Exit(code=1)

        def _mutate(s):
            s.tasks[downstream].depends_on = [
                e for e in s.tasks[downstream].depends_on
                if e.upstream_slug != upstream_slug
            ]

        state_mgr.mutate(_mutate)
        if output.is_tty:
            output.success(f"{downstream} no longer depends on {upstream_slug}")
        else:
            output.json({"downstream": downstream, "upstream": upstream_slug, "removed": True})
```

- [ ] **Step 4: Verify pass**

```bash
pytest tests/cli/test_depends.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/depends.py tests/cli/test_depends.py
git commit -m "feat(cli): mship depends remove (#104)"
mship journal "mship depends remove landed; loud error on missing edge" --action committed
```

---

## Task 6: `mship depends list` command

**Files:**
- Modify: `src/mship/cli/depends.py`
- Modify: `tests/cli/test_depends.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/cli/test_depends.py`:

```python
def test_depends_list_task_scoped(workspace, configured_app):
    """Without --graph, list shows the resolved task's upstream + downstream."""
    from mship.core.state import DependencyEdge
    a = _task("a")
    b = _task("b")
    c = _task("c")
    b.depends_on = [DependencyEdge(upstream_slug="a", created_at=datetime.now(timezone.utc))]
    c.depends_on = [DependencyEdge(upstream_slug="b", created_at=datetime.now(timezone.utc))]
    _seed(workspace, a, b, c)

    result = runner.invoke(app, ["depends", "list", "--task", "b"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["task"] == "b"
    assert [u["slug"] for u in data["upstream"]] == ["a"]
    assert [d["slug"] for d in data["downstream"]] == ["c"]


def test_depends_list_graph_emits_workspace_dag(workspace, configured_app):
    """--graph emits all tasks + edges."""
    from mship.core.state import DependencyEdge
    a = _task("a")
    b = _task("b")
    b.depends_on = [DependencyEdge(upstream_slug="a", created_at=datetime.now(timezone.utc))]
    _seed(workspace, a, b)

    result = runner.invoke(app, ["depends", "list", "--graph"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert {n["slug"] for n in data["nodes"]} == {"a", "b"}
    assert {(e["downstream"], e["upstream"]) for e in data["edges"]} == {("b", "a")}
```

- [ ] **Step 2: Verify failure**

```bash
pytest tests/cli/test_depends.py -k list -v
```

Expected: FAIL.

- [ ] **Step 3: Implement `list`**

In `src/mship/cli/depends.py`, append inside `register`:

```python
    @depends_app.command("list")
    def list_cmd(
        task: Optional[str] = typer.Option(None, "--task", help="Task slug to scope to."),
        graph: bool = typer.Option(False, "--graph", help="Emit the full workspace DAG instead of one task's edges."),
    ):
        """List dependencies for a task (default) or the whole workspace (--graph)."""
        from mship.core.task_graph import downstream_of

        output = Output()
        container = get_container()
        state = container.state_manager().load()

        if graph:
            nodes = [{"slug": s} for s in sorted(state.tasks.keys())]
            edges = []
            for slug, t in state.tasks.items():
                for e in t.depends_on:
                    edges.append({"downstream": slug, "upstream": e.upstream_slug})
            edges.sort(key=lambda e: (e["downstream"], e["upstream"]))
            payload = {"nodes": nodes, "edges": edges}
            if output.is_tty:
                _render_graph_tty(output, payload)
            else:
                output.json(payload)
            return

        resolved = resolve_for_command("depends", state, task, output)
        slug = resolved.task.slug
        upstream = [{"slug": e.upstream_slug} for e in state.tasks[slug].depends_on]
        downstream = [{"slug": s} for s in sorted(downstream_of(state, slug))]
        payload = {"task": slug, "upstream": upstream, "downstream": downstream}
        if output.is_tty:
            _render_task_deps_tty(output, payload)
        else:
            output.json(payload)
```

Add helper renderers at module-level (above `register`):

```python
def _render_task_deps_tty(output, payload):
    output.print(f"Task: {payload['task']}")
    output.print("Upstream:")
    if not payload["upstream"]:
        output.print("  (none)")
    else:
        for u in payload["upstream"]:
            output.print(f"  → {u['slug']}")
    output.print("Downstream:")
    if not payload["downstream"]:
        output.print("  (none)")
    else:
        for d in payload["downstream"]:
            output.print(f"  ← {d['slug']}")


def _render_graph_tty(output, payload):
    output.print("Workspace DAG:")
    if not payload["edges"]:
        output.print("  (no edges)")
        return
    for e in payload["edges"]:
        output.print(f"  {e['downstream']} → {e['upstream']}")
```

- [ ] **Step 4: Verify pass**

```bash
pytest tests/cli/test_depends.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/depends.py tests/cli/test_depends.py
git commit -m "feat(cli): mship depends list (--graph) (#104)"
mship journal "mship depends list command landed; task-scoped and --graph modes" --action committed
```

---

## Task 7: `mship spawn --depends-on` integration

**Files:**
- Modify: `src/mship/cli/worktree.py`
- Modify: `tests/cli/test_worktree.py` (or wherever spawn is tested today)

- [ ] **Step 1: Locate and study spawn**

```bash
grep -n "def spawn\|--depends" src/mship/cli/worktree.py | head
```

The flag list is around `src/mship/cli/worktree.py:212`. The handler persists a new `Task` via the state manager after validation/audit/setup.

- [ ] **Step 2: Add the failing test**

Append to `tests/cli/test_worktree.py` (or the closest spawn-test file):

```python
def test_spawn_with_depends_on_persists_edges(workspace_with_git, configured_app, monkeypatch):
    """spawn --depends-on a,b creates the task with edges to a and b."""
    # Seed an upstream task first.
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone
    sm = StateManager(workspace_with_git / ".mothership")
    sm.save(WorkspaceState(tasks={
        "a": Task(slug="a", description="d", phase="dev",
                  created_at=datetime.now(timezone.utc),
                  affected_repos=["mothership"], branch="feat/a"),
    }))

    result = runner.invoke(
        app,
        ["spawn", "downstream task", "--slug", "down", "--depends-on", "a", "--skip-setup"],
    )
    assert result.exit_code == 0, result.stderr or result.output
    state = StateManager(workspace_with_git / ".mothership").load()
    edges = state.tasks["down"].depends_on
    assert [e.upstream_slug for e in edges] == ["a"]


def test_spawn_with_unknown_depends_on_errors(workspace_with_git, configured_app):
    """spawn --depends-on <unknown> errors before creating the task."""
    result = runner.invoke(
        app,
        ["spawn", "x", "--slug", "x", "--depends-on", "nope", "--skip-setup"],
    )
    assert result.exit_code != 0
    assert "nope" in (result.stderr or result.output).lower()
```

(If `workspace_with_git` doesn't exist, use the spawn-test fixture used by the file's existing tests.)

- [ ] **Step 3: Verify failure**

```bash
pytest tests/cli/test_worktree.py -k "depends_on" -v
```

Expected: FAIL — flag unknown.

- [ ] **Step 4: Add the `--depends-on` flag**

In `src/mship/cli/worktree.py`, in the `spawn` signature (around line 212), add a new option:

```python
        depends_on: Optional[str] = typer.Option(
            None, "--depends-on",
            help="Comma-separated upstream task slugs this task depends on. See #104.",
        ),
```

After audit succeeds and BEFORE the task is persisted, validate the upstream slugs and compute edges:

```python
        # --- #104 dependency edges ---
        from datetime import datetime, timezone
        from mship.core.state import DependencyEdge
        from mship.core.task_graph import find_cycle

        edges: list[DependencyEdge] = []
        if depends_on:
            requested = [s.strip() for s in depends_on.split(",") if s.strip()]
            existing_state = container.state_manager().load()
            known = set(existing_state.tasks.keys())
            unknown = [s for s in requested if s not in known]
            if unknown:
                listing = ", ".join(sorted(known)) or "(none)"
                output.error(
                    f"Unknown upstream task(s): {', '.join(unknown)}. Known: {listing}."
                )
                raise typer.Exit(code=1)
            slug_for_cycle = slug if slug else _make_slug_from_description(description)
            for up in requested:
                cycle = find_cycle(existing_state, downstream=slug_for_cycle, new_upstream=up)
                if cycle is not None:
                    output.error(f"Cycle detected: {' → '.join(cycle)}")
                    raise typer.Exit(code=1)
            now = datetime.now(timezone.utc)
            edges = [DependencyEdge(upstream_slug=s, created_at=now) for s in requested]
```

When constructing the new `Task` (search for `Task(` in the spawn body), pass `depends_on=edges`. If the task is built incrementally via a builder/mutate function, pass `edges` through to the same place.

> Note: `_make_slug_from_description` may already exist (slugify helper); reuse the same routine `spawn` uses to derive the slug from description. If it's inline, factor or duplicate the minimal logic.

- [ ] **Step 5: Verify pass**

```bash
pytest tests/cli/test_worktree.py -k "depends_on" -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_worktree.py
git commit -m "feat(spawn): --depends-on <slug,...> with cycle + unknown-upstream checks (#104)"
mship journal "spawn --depends-on landed; rejects unknown and cycles" --action committed
```

---

## Task 8: `mship finish` blocked-by-deps check

**Files:**
- Modify: `src/mship/cli/worktree.py` (the `finish` command, around line 682)
- Modify: `tests/cli/test_worktree.py`

The block fires before any push. Bypass with `--bypass-deps`. Readiness uses the cached reconcile decisions (or a fresh reconcile if no cache).

- [ ] **Step 1: Add failing tests**

Append to `tests/cli/test_worktree.py` (or finish-specific test file):

```python
def test_finish_blocked_by_unready_upstream(workspace_with_git, configured_app, monkeypatch):
    """finish refuses when a hard upstream isn't merged."""
    from mship.core.state import StateManager, Task, WorkspaceState, DependencyEdge
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    sm = StateManager(workspace_with_git / ".mothership")
    sm.save(WorkspaceState(tasks={
        "a": Task(slug="a", description="a", phase="dev",
                  created_at=now, affected_repos=["mothership"], branch="feat/a"),
        "b": Task(slug="b", description="b", phase="dev",
                  created_at=now, affected_repos=["mothership"], branch="feat/b",
                  depends_on=[DependencyEdge(upstream_slug="a", created_at=now)]),
    }))

    # Stub reconcile so that a is NOT merged.
    from mship.core.reconcile.gate import Decision
    from mship.core.reconcile.detect import UpstreamState
    def _fake_decisions(state):
        return {"a": Decision(slug="a", state=UpstreamState.in_sync,
                              pr_url=None, pr_number=None, base=None, merge_commit=None)}
    monkeypatch.setattr("mship.cli.worktree._dependency_decisions", _fake_decisions, raising=False)

    result = runner.invoke(app, ["finish", "--task", "b"])
    assert result.exit_code != 0
    err = (result.stderr or result.output).lower()
    assert "a" in err and ("not ready" in err or "blocked" in err)


def test_finish_bypass_deps(workspace_with_git, configured_app, monkeypatch):
    """--bypass-deps proceeds past the upstream check."""
    # ... same seeding as above, then:
    result = runner.invoke(app, ["finish", "--task", "b", "--bypass-deps"])
    # Will likely fail downstream (no real git state) — assert only that we PASSED
    # the deps gate (i.e. the error message does NOT mention upstream a).
    err = (result.stderr or result.output).lower()
    assert "depends on" not in err
```

- [ ] **Step 2: Verify failure**

```bash
pytest tests/cli/test_worktree.py -k "blocked_by_unready\|bypass_deps" -v
```

Expected: FAIL — no upstream check exists.

- [ ] **Step 3: Add the deps gate to `finish`**

In `src/mship/cli/worktree.py`, near the top of the `finish` function (around line 682), after task resolution but before any push:

```python
        bypass_deps: bool = typer.Option(False, "--bypass-deps", help="Skip the dependency-readiness check (#104)."),
```
(add this to the `finish` signature alongside the other options)

Then in the body, right after `resolve_for_command(...)` returns the task:

```python
        # --- #104 dependency-readiness gate ---
        if task.depends_on and not bypass_deps:
            decisions = _dependency_decisions(state)
            from mship.core.task_graph import is_ready
            blocked_by = [
                edge.upstream_slug
                for edge in task.depends_on
                if not is_ready(state, edge.upstream_slug, decisions)
            ]
            if blocked_by:
                output.error(
                    f"finish blocked: upstream task(s) not ready: {', '.join(blocked_by)}.\n"
                    f"  Run `mship status --task <slug>` for state, "
                    f"or pass --bypass-deps to override."
                )
                raise typer.Exit(code=1)
```

Add the helper at module level (so tests can monkeypatch it):

```python
def _dependency_decisions(state):
    """Return cached reconcile decisions for use by the deps gate.

    Kept in a function so tests can stub it.
    """
    from mship.core.reconcile.cache import ReconcileCache
    from mship.core.reconcile.gate import reconcile_now
    from mship.core.reconcile.fetch import fetch_pr_snapshots, collect_git_snapshots

    cache = ReconcileCache(_state_dir_for_decisions())
    def _fetcher(branches, worktrees_by_branch):
        return (fetch_pr_snapshots(branches), collect_git_snapshots(worktrees_by_branch))
    try:
        return reconcile_now(state, cache=cache, fetcher=_fetcher)
    except Exception:
        return {}


def _state_dir_for_decisions():
    """Return the state dir for ReconcileCache use in finish gate."""
    from mship.cli import container
    return container.state_dir()
```

(Adjust container access to match the existing pattern in `worktree.py`.)

- [ ] **Step 4: Verify pass**

```bash
pytest tests/cli/test_worktree.py -k "blocked_by_unready\|bypass_deps" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_worktree.py
git commit -m "feat(finish): refuse when upstream task(s) not ready; --bypass-deps override (#104)"
mship journal "finish blocked-by-deps gate landed; --bypass-deps works" --action committed
```

---

## Task 9: `mship close` downstream check + `--cascade` / `--detach-downstream`

**Files:**
- Modify: `src/mship/cli/worktree.py` (the `close` command, around line 430)
- Modify: `tests/cli/test_worktree.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/cli/test_worktree.py`:

```python
def test_close_with_downstream_non_tty_refuses(workspace_with_git, configured_app):
    """Non-TTY close refuses when downstream tasks exist."""
    from mship.core.state import StateManager, Task, WorkspaceState, DependencyEdge
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    StateManager(workspace_with_git / ".mothership").save(WorkspaceState(tasks={
        "a": Task(slug="a", description="a", phase="dev", finished_at=now,
                  created_at=now, affected_repos=["mothership"], branch="feat/a"),
        "b": Task(slug="b", description="b", phase="dev",
                  created_at=now, affected_repos=["mothership"], branch="feat/b",
                  depends_on=[DependencyEdge(upstream_slug="a", created_at=now)]),
    }))

    result = runner.invoke(app, ["close", "a", "--yes", "--skip-pr-check"])
    assert result.exit_code != 0
    err = (result.stderr or result.output).lower()
    assert "downstream" in err and "b" in err
    assert "--cascade" in err or "--detach-downstream" in err


def test_close_detach_downstream_clears_edges(workspace_with_git, configured_app):
    """--detach-downstream clears the inbound edges but leaves downstream alive."""
    # ... seed as above
    result = runner.invoke(app, ["close", "a", "--yes", "--skip-pr-check", "--detach-downstream"])
    assert result.exit_code == 0
    state = StateManager(workspace_with_git / ".mothership").load()
    assert "a" not in state.tasks
    assert state.tasks["b"].depends_on == []


def test_close_cascade_removes_downstream(workspace_with_git, configured_app):
    """--cascade closes both."""
    # ... seed as above
    result = runner.invoke(app, ["close", "a", "--yes", "--skip-pr-check", "--cascade"])
    assert result.exit_code == 0
    state = StateManager(workspace_with_git / ".mothership").load()
    assert state.tasks == {}
```

- [ ] **Step 2: Verify failure**

```bash
pytest tests/cli/test_worktree.py -k "close.*downstream\|cascade\|detach_downstream" -v
```

Expected: FAIL.

- [ ] **Step 3: Add the flags and the check**

In `src/mship/cli/worktree.py`, in `close`'s signature (around line 430-446), add:

```python
        cascade: bool = typer.Option(False, "--cascade", help="Also close downstream tasks (#104)."),
        detach_downstream: bool = typer.Option(
            False, "--detach-downstream",
            help="Clear the inbound dependency edges, leave downstream tasks alive (#104).",
        ),
```

After task resolution (early in the function, before any teardown):

```python
        # --- #104 downstream check ---
        from mship.core.task_graph import downstream_of
        downstream = sorted(downstream_of(state, task.slug))
        if downstream and not force:
            if cascade and detach_downstream:
                output.error("Pass only one of --cascade / --detach-downstream.")
                raise typer.Exit(code=1)
            if not (cascade or detach_downstream):
                if output.is_tty:
                    choice = typer.prompt(
                        f"Task {task.slug!r} has downstream tasks: {', '.join(downstream)}. "
                        "[c]ascade close downstream, [d]etach edges, [a]bort",
                        default="a",
                    ).strip().lower()
                    if choice.startswith("c"):
                        cascade = True
                    elif choice.startswith("d"):
                        detach_downstream = True
                    else:
                        output.error("Aborted.")
                        raise typer.Exit(code=1)
                else:
                    output.error(
                        f"close refused: downstream tasks depend on {task.slug!r}: "
                        f"{', '.join(downstream)}. Pass --cascade (close them too) "
                        "or --detach-downstream (clear the edges) to proceed."
                    )
                    raise typer.Exit(code=1)
```

After the close completes successfully (state has been mutated to remove `task.slug`), handle the chosen mode:

```python
        if downstream and detach_downstream:
            def _detach(s):
                for d_slug in downstream:
                    t = s.tasks.get(d_slug)
                    if t is None:
                        continue
                    t.depends_on = [e for e in t.depends_on if e.upstream_slug != task.slug]
            state_mgr.mutate(_detach)
        elif downstream and cascade:
            # Recursively close downstream tasks. Simplest: invoke the close
            # logic per-downstream. For v1, mark each downstream's depends_on
            # to drop the edge, then delete the task entry.
            def _cascade(s):
                for d_slug in downstream:
                    s.tasks.pop(d_slug, None)
            state_mgr.mutate(_cascade)
```

> **Note for the implementer:** cascade-close in v1 simply removes downstream tasks from state. Their worktrees are NOT torn down by `--cascade` in this implementation (out of scope for v1). The flag is honored at the state level; the user can `mship prune` to clean orphaned worktrees afterward. Document this in the help text.

Update the cascade flag's help string accordingly:

```python
        cascade: bool = typer.Option(
            False, "--cascade",
            help="Also remove downstream tasks from state (#104). "
                 "Their worktrees stay until `mship prune`.",
        ),
```

- [ ] **Step 4: Verify pass**

```bash
pytest tests/cli/test_worktree.py -k "close.*downstream\|cascade\|detach_downstream" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_worktree.py
git commit -m "feat(close): downstream check with --cascade / --detach-downstream (#104)"
mship journal "close downstream-check landed; cascade removes from state, detach clears edges" --action committed
```

---

## Task 10: `mship status` — `dependencies` block under `resolved_task`

**Files:**
- Modify: `src/mship/cli/status.py`
- Modify: `tests/cli/test_status.py`

- [ ] **Step 1: Add failing test**

Append to `tests/cli/test_status.py`:

```python
def test_status_dependencies_block_present(workspace, configured_app):
    """resolved_task.dependencies includes upstream, downstream, blocked, blocked_by."""
    from mship.core.state import DependencyEdge, StateManager, Task, WorkspaceState
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    StateManager(workspace / ".mothership").save(WorkspaceState(tasks={
        "a": Task(slug="a", description="a", phase="dev",
                  created_at=now, affected_repos=["mothership"], branch="feat/a"),
        "b": Task(slug="b", description="b", phase="dev",
                  created_at=now, affected_repos=["mothership"], branch="feat/b",
                  depends_on=[DependencyEdge(upstream_slug="a", created_at=now)]),
        "c": Task(slug="c", description="c", phase="dev",
                  created_at=now, affected_repos=["mothership"], branch="feat/c",
                  depends_on=[DependencyEdge(upstream_slug="b", created_at=now)]),
    }))

    result = runner.invoke(app, ["status", "--task", "b"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    deps = data["resolved_task"]["dependencies"]
    assert [u["slug"] for u in deps["upstream"]] == ["a"]
    assert [d["slug"] for d in deps["downstream"]] == ["c"]
    # ready is False because reconcile won't say merged for an unfinished task
    assert deps["blocked"] is True
    assert deps["blocked_by"] == ["a"]
```

- [ ] **Step 2: Verify failure**

```bash
pytest tests/cli/test_status.py -k dependencies_block -v
```

Expected: FAIL — key missing.

- [ ] **Step 3: Build the dependencies payload in `status.py`**

In `src/mship/cli/status.py`, where `resolved_task` is built, add:

```python
        # --- #104 dependency block ---
        from mship.core.task_graph import downstream_of, is_ready
        decisions = {}  # avoid expensive reconcile during status; readiness based on state alone for now
        deps_upstream = []
        blocked_by: list[str] = []
        for edge in task.depends_on:
            ready = is_ready(state, edge.upstream_slug, decisions)
            deps_upstream.append({"slug": edge.upstream_slug, "ready": ready})
            if not ready:
                blocked_by.append(edge.upstream_slug)
        deps_downstream = [{"slug": s} for s in sorted(downstream_of(state, task.slug))]
        resolved_task_dict["dependencies"] = {
            "upstream": deps_upstream,
            "downstream": deps_downstream,
            "blocked": bool(blocked_by),
            "blocked_by": blocked_by,
        }
```

> **Performance note:** `status` is called frequently. We deliberately use an empty `reconcile_decisions` dict here — `is_ready` will return False for any task that hasn't been confirmed merged, which is the safe-side answer ("treat as blocked"). The `finish` gate does the real reconcile pull. If you want a richer readiness signal in `status`, pass `decisions = ReconcileCache(...).read()?.decisions` and skip the network fetch.

- [ ] **Step 4: Verify pass**

```bash
pytest tests/cli/test_status.py -v
```

Expected: PASS for new test; existing envelope tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/status.py tests/cli/test_status.py
git commit -m "feat(status): dependencies block under resolved_task (#104)"
mship journal "status envelope grew resolved_task.dependencies; tests passing" --action committed
```

---

## Task 11: `mship dispatch` — `## Dependencies` section in prompt body

**Files:**
- Modify: `src/mship/core/dispatch.py`
- Modify: `tests/cli/test_dispatch.py` (or `tests/core/test_dispatch.py` — whichever covers the prompt template)

- [ ] **Step 1: Add failing test**

Append to the appropriate dispatch test:

```python
def test_dispatch_prompt_includes_dependencies_section(workspace, configured_app):
    from mship.core.state import StateManager, Task, WorkspaceState, DependencyEdge
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    StateManager(workspace / ".mothership").save(WorkspaceState(tasks={
        "a": Task(slug="a", description="a", phase="dev",
                  created_at=now, affected_repos=["mothership"], branch="feat/a"),
        "b": Task(slug="b", description="b", phase="dev",
                  created_at=now, affected_repos=["mothership"], branch="feat/b",
                  worktrees={"mothership": workspace / ".worktrees" / "b" / "mothership"},
                  depends_on=[DependencyEdge(upstream_slug="a", created_at=now)]),
    }))

    result = runner.invoke(app, ["dispatch", "--task", "b", "--instruction", "go"])
    assert result.exit_code == 0
    assert "## Dependencies" in result.stdout
    assert "a" in result.stdout
```

- [ ] **Step 2: Verify failure**

Expected: FAIL — no `## Dependencies` block in output.

- [ ] **Step 3: Add the section to `build_dispatch_prompt`**

In `src/mship/core/dispatch.py`, in `build_dispatch_prompt`, find the section assembly. Add a helper:

```python
def _format_dependencies_section(task) -> str:
    if not task.depends_on:
        return ""
    lines = ["## Dependencies", ""]
    for edge in task.depends_on:
        lines.append(f"- depends on: `{edge.upstream_slug}`")
    lines.append("")
    return "\n".join(lines)
```

Insert the section output after the "## Task" / before the journal section (location-specific — match the existing template style).

- [ ] **Step 4: Verify pass**

```bash
pytest tests/cli/test_dispatch.py -k "Dependencies" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/dispatch.py tests/cli/test_dispatch.py
git commit -m "feat(dispatch): include ## Dependencies section in prompt body (#104)"
mship journal "dispatch prompt grew ## Dependencies block" --action committed
```

---

## Task 12: `mship reconcile` — `dependency_stale` state

**Files:**
- Modify: `src/mship/core/reconcile/detect.py`
- Modify: `src/mship/core/reconcile/gate.py` (where Decisions are finalized for the workspace)
- Modify: `src/mship/cli/reconcile.py`
- Modify: `tests/core/reconcile/test_detect.py` (or the closest test file)

A downstream task is `dependency_stale` when its current state would otherwise be `in_sync` or `merged`, but an upstream merged after the downstream task was created. Implementation: post-process decisions; override only when the downstream is currently `in_sync`.

- [ ] **Step 1: Add failing test**

Append to `tests/core/reconcile/test_detect.py`:

```python
def test_dependency_stale_when_upstream_merged_after_downstream_created():
    """Downstream task → in_sync; upstream merged after downstream created → dependency_stale."""
    from datetime import datetime, timezone, timedelta
    from mship.core.state import DependencyEdge, Task, WorkspaceState
    from mship.core.reconcile.detect import UpstreamState
    from mship.core.reconcile.gate import Decision
    from mship.core.reconcile.dependency_stale import apply_dependency_stale

    t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 10, tzinfo=timezone.utc)
    state = WorkspaceState(tasks={
        "a": Task(slug="a", description="a", phase="dev",
                  created_at=t0, affected_repos=["r"], branch="feat/a",
                  finished_at=t0),
        "b": Task(slug="b", description="b", phase="dev",
                  created_at=t0, affected_repos=["r"], branch="feat/b",
                  depends_on=[DependencyEdge(upstream_slug="a", created_at=t0)]),
    })
    decisions = {
        "a": Decision(slug="a", state=UpstreamState.merged, pr_url=None,
                      pr_number=None, base=None, merge_commit=None, merge_at=t1),
        "b": Decision(slug="b", state=UpstreamState.in_sync, pr_url=None,
                      pr_number=None, base=None, merge_commit=None, merge_at=None),
    }

    out = apply_dependency_stale(state, decisions)
    assert out["b"].state == UpstreamState.dependency_stale
```

(If `Decision` doesn't currently carry merge_at, the post-processing can use `decision.updated_at` as an approximation — adjust the test accordingly.)

- [ ] **Step 2: Verify failure**

```bash
pytest tests/core/reconcile/test_detect.py -k dependency_stale -v
```

Expected: FAIL — module / enum value missing.

- [ ] **Step 3: Extend the enum and add the post-process**

In `src/mship/core/reconcile/detect.py`, add to the `UpstreamState` enum:

```python
    dependency_stale = "dependency_stale"
```

Create `src/mship/core/reconcile/dependency_stale.py`:

```python
"""Post-process reconcile decisions to surface dependency_stale states (#104)."""
from __future__ import annotations

from datetime import datetime

from mship.core.reconcile.detect import UpstreamState


def apply_dependency_stale(state, decisions: dict) -> dict:
    """Override `in_sync` decisions to `dependency_stale` when any upstream
    has merged AFTER the downstream's task.created_at.

    Returns a new dict; does not mutate the input.
    """
    out = dict(decisions)
    for slug, task in state.tasks.items():
        d = out.get(slug)
        if d is None or d.state != UpstreamState.in_sync:
            continue
        for edge in task.depends_on:
            up = out.get(edge.upstream_slug)
            if up is None or up.state != UpstreamState.merged:
                continue
            up_merge_time = getattr(up, "merge_at", None) or _parse(getattr(up, "updated_at", None))
            if up_merge_time is None:
                continue
            if up_merge_time > task.created_at:
                out[slug] = d.__class__(**{**d.__dict__, "state": UpstreamState.dependency_stale})
                break
    return out


def _parse(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
```

In `src/mship/core/reconcile/gate.py`, where `reconcile_now` builds the final decisions dict, apply the post-process at the end:

```python
from mship.core.reconcile.dependency_stale import apply_dependency_stale
# ...
decisions = apply_dependency_stale(state, decisions)
return decisions
```

In `src/mship/cli/reconcile.py`, extend the action-hint map:

```python
_ACTION_HINTS = {
    UpstreamState.merged:           "run `mship close`",
    UpstreamState.closed:           "run `mship close --abandon`",
    UpstreamState.diverged:         "pull and rebase",
    UpstreamState.base_changed:     "rebase onto new base",
    UpstreamState.missing:          "—",
    UpstreamState.in_sync:          "—",
    UpstreamState.dependency_stale: "rebase onto upstream's merge",
}
```

And ensure `_glyph` covers the new state:

```python
def _glyph(state: UpstreamState) -> str:
    return "✓" if state in (UpstreamState.in_sync, UpstreamState.missing) else "⚠"
```

(Already correct — non-in_sync states get `⚠`. The new state falls through to `⚠`.)

- [ ] **Step 4: Verify pass**

```bash
pytest tests/core/reconcile/ -v
```

Expected: PASS for new test; existing reconcile tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/reconcile/ src/mship/cli/reconcile.py tests/core/reconcile/
git commit -m "feat(reconcile): dependency_stale state when upstream merged after downstream created (#104)"
mship journal "reconcile dependency_stale post-process landed; tests green" --action committed
```

---

## Task 13: Documentation — skill + README + AGENTS + GEMINI

**Files:**
- Modify: `src/mship/skills/working-with-mothership/SKILL.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `GEMINI.md`

- [ ] **Step 1: Update the skill**

In `src/mship/skills/working-with-mothership/SKILL.md`, find the command reference section. Add a new subsection after `### Working on a task`:

````markdown
### Task dependencies

Express that task B depends on task A. `finish` refuses to ship B until every upstream is merged.

```bash
mship spawn "downstream work" --depends-on a,b          # declare at spawn
mship depends add <upstream-slug> [--task <slug>]       # retrofit on an existing task
mship depends remove <upstream-slug> [--task <slug>]
mship depends list [--task <slug>] [--graph]            # --graph = full workspace DAG
mship finish --bypass-deps                              # override the readiness gate
mship close --cascade        # also remove downstream from state
mship close --detach-downstream   # clear inbound edges, leave downstream alive
```

`mship status` exposes the graph under `.resolved_task.dependencies`:

```bash
mship status | jq .resolved_task.dependencies
# { "upstream": [...], "downstream": [...], "blocked": bool, "blocked_by": [...] }
```

`mship dispatch` includes a `## Dependencies` section in the subagent prompt body. `mship reconcile` reports `dependency_stale` for a downstream that's in sync but whose upstream merged after the downstream was created (i.e., the downstream needs a rebase).

No soft/advisory edges in v1 — for "informed by task-a" relationships, use `mship journal`.
````

- [ ] **Step 2: Update README cheat sheet**

In `README.md`, add to the cheat-sheet section near the other workflow commands:

```text
mship depends add/remove/list      # manage task-to-task dependency edges (#104)
mship spawn --depends-on a,b       # declare upstream task(s) at spawn time
mship finish --bypass-deps         # ship a downstream even if upstream isn't ready
```

- [ ] **Step 3: Update AGENTS.md and GEMINI.md**

In both files, add a one-liner pointing at the new verb:

```text
- `mship depends`: declare/inspect task-to-task dependencies (#104).
```

- [ ] **Step 4: Commit**

```bash
git add src/mship/skills/working-with-mothership/SKILL.md README.md AGENTS.md GEMINI.md
git commit -m "docs: document task dependency graph (#104)"
mship journal "docs updated: skill, README, AGENTS, GEMINI" --action committed
```

---

## Task 14: End-to-end integration test

**Files:**
- Create: `tests/test_dependency_integration.py`

- [ ] **Step 1: Add the integration test**

Create `tests/test_dependency_integration.py`:

```python
"""End-to-end: spawn → finish-blocked → finish-upstream → finish-downstream (#104)."""
from __future__ import annotations
import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager

runner = CliRunner()


def test_dependency_flow(workspace_with_git, configured_app, monkeypatch):
    # 1. Spawn task-a.
    r1 = runner.invoke(app, ["spawn", "task a", "--slug", "a", "--skip-setup"])
    assert r1.exit_code == 0, r1.stderr or r1.output

    # 2. Spawn task-b depending on a.
    r2 = runner.invoke(app, ["spawn", "task b", "--slug", "b", "--depends-on", "a", "--skip-setup"])
    assert r2.exit_code == 0, r2.stderr or r2.output

    # 3. Status of b shows blocked_by=[a].
    r3 = runner.invoke(app, ["status", "--task", "b"])
    data = json.loads(r3.stdout)
    assert data["resolved_task"]["dependencies"]["blocked"] is True
    assert data["resolved_task"]["dependencies"]["blocked_by"] == ["a"]

    # 4. Stub a as ready, then bypass: finish b succeeds without bypass.
    # (Real finish requires PR mocking — sufficient here to confirm the gate fires
    #  before any push; --bypass-deps clears it.)
    r4 = runner.invoke(app, ["finish", "--task", "b"])
    err = (r4.stderr or r4.output).lower()
    assert "upstream" in err or "blocked" in err

    r5 = runner.invoke(app, ["finish", "--task", "b", "--bypass-deps"])
    # bypass clears the deps gate; downstream errors (no PR setup) are fine —
    # we only assert the deps message is gone.
    err = (r5.stderr or r5.output).lower()
    assert "blocked" not in err and "depends on" not in err
```

- [ ] **Step 2: Run the test**

```bash
pytest tests/test_dependency_integration.py -v
```

Expected: PASS.

- [ ] **Step 3: Run the full suite**

```bash
pytest -x
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_dependency_integration.py
git commit -m "test(integration): dependency flow end-to-end (#104)"
mship journal "integration test landed; full flow tested" --action committed
```

---

## Verification before finish

- [ ] **Run the full test suite**

```bash
pytest
```

Expected: all PASS.

- [ ] **Confirm the spec is satisfied**

Skim `docs/superpowers/specs/2026-05-14-task-dependency-graph-design.md`. For each section, confirm a corresponding task implemented it:

- State model → Task 1
- Readiness signal → Task 3
- CLI surface (`mship depends`) → Tasks 4, 5, 6
- `spawn --depends-on` → Task 7
- `finish --bypass-deps` → Task 8
- `close --cascade` / `--detach-downstream` → Task 9
- `status` dependencies block → Task 10
- `dispatch ## Dependencies` → Task 11
- `reconcile dependency_stale` → Task 12
- Cycle detection → Task 2
- Error handling → covered across Tasks 4-9
- Docs → Task 13
- Integration → Task 14

- [ ] **Run `mship finish`**

```bash
mship finish --body-file docs/superpowers/specs/2026-05-14-task-dependency-graph-design.md
```

(Or write a Summary + Test plan into a file and pass it via `--body-file`.)
