"""Dispatch an approved spec to a task (the A6/B4 dispatch path).

`dispatch_spec` is the shared core behind `mship spec dispatch` (CLI) and the
`POST /specs/{id}/dispatch` serve endpoint. It auto-spawns a task when none
exists yet, via an injected `spawn_fn` — real callers pass a thunk over
`WorktreeManager.spawn` (real git + state mutation); tests pass a fake. Keeping
the spawn dependency injected is what makes this unit-testable, which is exactly
what blocked auto-spawn in the original A6 fallback (see MOS-150 / MOS-171).
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
) -> DispatchResult:
    """Bind an approved spec to its task (auto-spawning one if needed).

    - Requires `spec.status == "approved"` (else `DispatchError`).
    - If a task with slug `spec.id` exists, binds to it; otherwise calls
      `spawn_fn(spec)` to create the task + worktrees (requires non-empty
      `affected_repos`).
    - Sets `task.spec_id = spec.id`, transitions the spec to `dispatched`,
      stamps `task_slug`/`updated_at`, and persists the spec.
    """
    if spec.status != "approved":
        raise DispatchError(
            f"spec {spec.id!r} is {spec.status!r} — approve it first "
            f"(mship spec approve {spec.id})."
        )

    state = state_manager.load()
    if spec.id in state.tasks:
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

    # Bind the task to the spec under the state lock.
    def _bind(s):
        if spec.id in s.tasks:
            s.tasks[spec.id].spec_id = spec.id
    state_manager.mutate(_bind)

    spec.status = "dispatched"
    spec.task_slug = spec.id
    spec.updated_at = now
    store.save(spec)

    return DispatchResult(
        spec=spec, task=task, spawned=spawned,
        handoff=build_dispatch_handoff(spec, task),
    )
