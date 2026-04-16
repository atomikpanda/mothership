"""Resolve which task a CLI invocation targets.

Priority: --task flag > MSHIP_TASK env > cwd → worktree → task.

Fallbacks when no anchor resolves:
  - 0 tasks       → NoActiveTaskError
  - exactly 1     → return that task (zero-ambiguity UX win)
  - 2+ tasks      → AmbiguousTaskError
"""
from __future__ import annotations

from pathlib import Path

from mship.core.state import Task, WorkspaceState


class NoActiveTaskError(Exception):
    """No tasks exist in workspace state."""


class UnknownTaskError(Exception):
    """A named task (flag or env) doesn't exist in workspace state."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"Unknown task: {slug}")
        self.slug = slug


class AmbiguousTaskError(Exception):
    """Multiple active tasks and no anchor (cwd/env/flag) could disambiguate."""

    def __init__(self, active: list[str]) -> None:
        super().__init__(f"Multiple active tasks: {', '.join(active)}")
        self.active = active


def resolve_task(
    state: WorkspaceState,
    *,
    cli_task: str | None,
    env_task: str | None,
    cwd: Path,
) -> Task:
    # 1. Explicit --task flag wins.
    if cli_task is not None:
        if cli_task in state.tasks:
            return state.tasks[cli_task]
        raise UnknownTaskError(cli_task)

    # 2. MSHIP_TASK env var.
    if env_task:
        if env_task in state.tasks:
            return state.tasks[env_task]
        raise UnknownTaskError(env_task)

    # 3. Walk cwd upward — first match wins.
    cwd_resolved = cwd.resolve()
    for task in state.tasks.values():
        for wt_path in task.worktrees.values():
            wt_resolved = Path(wt_path).resolve()
            try:
                cwd_resolved.relative_to(wt_resolved)
                return task
            except ValueError:
                continue

    # 4. No anchor resolved.
    if not state.tasks:
        raise NoActiveTaskError(
            "no active task; run `mship spawn \"description\"` to start one"
        )
    # Exactly one active task → no ambiguity; just use it.
    if len(state.tasks) == 1:
        return next(iter(state.tasks.values()))
    raise AmbiguousTaskError(sorted(state.tasks.keys()))
