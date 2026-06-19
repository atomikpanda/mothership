"""Dispatch an approved spec to a task (the A6/B4 dispatch path).

`dispatch_spec` is the shared core behind `mship spec dispatch` (CLI) and the
`POST /specs/{id}/dispatch` serve endpoint. It binds an approved spec to a
task and transitions it to 'dispatched'.  Task selection order:

1. ``task_slug`` given → bind to that existing task (error if unknown, or if
   the spec is already bound to a *different* task).
2. Spec already bound (``spec.task_slug`` in state) → idempotent reuse.
3. A task named ``spec.id`` exists → bind to it.
4. Otherwise → auto-spawn via ``spawn_fn(spec)`` (requires ``affected_repos``).

The ``spawn_fn`` dependency is injected so real callers pass a thunk over
``WorktreeManager.spawn`` while tests use fakes (see MOS-150 / MOS-171).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from mship.core.spec import Spec
from mship.core.spec_body import parse_body_sections
from mship.core.state import Task


class DispatchError(Exception):
    """Spec is not in a dispatchable state (not approved, or un-spawnable)."""


@dataclass
class DispatchResult:
    spec: Spec
    task: Task
    spawned: bool       # True if a task was auto-spawned during this dispatch
    handoff: str        # the subagent handoff prompt


def build_dispatch_handoff(spec: Spec, task: Task) -> str:
    """Render the handoff prompt printed/returned after a successful dispatch."""
    sections = parse_body_sections(spec.body)
    problem = sections.get("Problem", "").strip()
    criteria_lines = "\n".join(
        f"  - [{ac.id}] {ac.text}" for ac in spec.acceptance_criteria
    )
    worktrees_lines = "\n".join(
        f"  - {repo}: {path}" for repo, path in task.worktrees.items()
    ) or "  (no worktrees registered yet)"
    return f"""\
# Spec dispatch: {spec.title}

**spec id:** {spec.id}
**task slug:** {task.slug}
**branch:** {task.branch}

## Problem

{problem or "(see spec body)"}

## Acceptance criteria

{criteria_lines or "  (none)"}

## Worktrees

{worktrees_lines}

Run `mship dispatch --task {task.slug} --instruction "<your instruction>"` to emit a full subagent prompt.
"""


def dispatch_spec(
    spec: Spec,
    *,
    state_manager,
    store,
    spawn_fn: Callable[[Spec], Task],
    now: datetime,
    task_slug: str | None = None,
) -> DispatchResult:
    """Bind an approved (or already-dispatched) spec to a task.

    Task selection, in order:
    - `task_slug` given: bind to that existing task (error if unknown, or if the
      spec is already bound to a *different* task).
    - spec already bound (`spec.task_slug` exists in state): reuse it (idempotent).
    - a task named `spec.id` exists: bind to it.
    - otherwise: auto-spawn via `spawn_fn(spec)` (requires `affected_repos`).
    """
    if spec.status not in ("approved", "dispatched"):
        raise DispatchError(
            f"spec {spec.id!r} is {spec.status!r} — approve it first "
            f"(mship spec approve {spec.id})."
        )

    state = state_manager.load()
    bound_slug = (
        spec.task_slug
        if spec.task_slug and spec.task_slug in state.tasks
        else None
    )

    if task_slug is not None:
        if task_slug not in state.tasks:
            raise DispatchError(
                f"--task {task_slug!r} not found. "
                f"Active tasks: {sorted(state.tasks)}."
            )
        if bound_slug is not None and bound_slug != task_slug:
            raise DispatchError(
                f"spec {spec.id!r} is already bound to task {bound_slug!r}; "
                f"refusing to rebind to {task_slug!r}. Drop --task to reuse it."
            )
        task = state.tasks[task_slug]
        spawned = False
    elif bound_slug is not None:
        task = state.tasks[bound_slug]
        spawned = False
    elif spec.id in state.tasks:
        task = state.tasks[spec.id]
        spawned = False
    else:
        if not spec.affected_repos:
            raise DispatchError(
                f"spec {spec.id!r} has no affected_repos; cannot auto-spawn a task. "
                f"Add repos to the spec or spawn a task named {spec.id!r} first."
            )
        task = spawn_fn(spec)
        spawned = True

    # Bind the chosen task to the spec under the state lock.
    chosen_slug = task.slug

    def _bind(s):
        if chosen_slug in s.tasks:
            s.tasks[chosen_slug].spec_id = spec.id

    state_manager.mutate(_bind)

    spec.status = "dispatched"
    spec.task_slug = chosen_slug
    spec.updated_at = now
    store.save(spec)

    return DispatchResult(
        spec=spec, task=task, spawned=spawned,
        handoff=build_dispatch_handoff(spec, task),
    )
