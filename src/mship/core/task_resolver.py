"""Resolve which task a CLI invocation targets.

Priority: --task flag > MSHIP_TASK env > cwd → worktree → task.

Fallbacks when no anchor resolves:
  - 0 tasks       → NoActiveTaskError
  - exactly 1     → return that task with ResolutionSource.SINGLE_ACTIVE
  - 2+ tasks      → AmbiguousTaskError(candidates=<all active tasks>)

Cwd ambiguity:
  - cwd matches 2+ distinct worktree paths → AmbiguousTaskError(candidates=<matches>)

Returns `(Task, ResolutionSource)` so callers can surface how the task was picked.
"""
from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from mship.core.state import Task, WorkspaceState


class ResolutionSource(StrEnum):
    CLI_FLAG = "--task"
    ENV_VAR = "MSHIP_TASK"
    CWD = "cwd"
    SINGLE_ACTIVE = "only active task"


class NoActiveTaskError(Exception):
    """No tasks exist in workspace state."""


class UnknownTaskError(Exception):
    """A named task (flag or env) doesn't exist in workspace state."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"Unknown task: {slug}")
        self.slug = slug


class AmbiguousTaskError(Exception):
    """Multiple tasks could apply and no anchor disambiguated.

    `candidates` carries `(slug, worktree_path)` tuples so callers can
    render concrete `--task <slug>` hints. `worktree_path` is the first
    worktree in the task's `worktrees` dict, or None if the task has none.
    """

    def __init__(
        self,
        active: list[str],
        candidates: list[tuple[str, Path | None]] | None = None,
    ) -> None:
        super().__init__(f"Multiple active tasks: {', '.join(active)}")
        self.active = active
        self.candidates = candidates if candidates is not None else []


def _first_worktree_path(task: Task) -> Path | None:
    for p in task.worktrees.values():
        return Path(p)
    return None


def resolve_task(
    state: WorkspaceState,
    *,
    cli_task: str | None,
    env_task: str | None,
    cwd: Path,
) -> tuple[Task, ResolutionSource]:
    # 1. Explicit --task flag wins.
    if cli_task is not None:
        if cli_task in state.tasks:
            return state.tasks[cli_task], ResolutionSource.CLI_FLAG
        raise UnknownTaskError(cli_task)

    # 2. MSHIP_TASK env var.
    if env_task:
        if env_task in state.tasks:
            return state.tasks[env_task], ResolutionSource.ENV_VAR
        raise UnknownTaskError(env_task)

    # 3. Walk cwd upward — collect all matches, not just the first.
    cwd_resolved = cwd.resolve()
    cwd_matches: list[tuple[str, Path]] = []
    seen_slugs: set[str] = set()
    for task in state.tasks.values():
        for wt_path in task.worktrees.values():
            wt_resolved = Path(wt_path).resolve()
            try:
                cwd_resolved.relative_to(wt_resolved)
            except ValueError:
                continue
            if task.slug not in seen_slugs:
                cwd_matches.append((task.slug, wt_resolved))
                seen_slugs.add(task.slug)
                break  # one match per task is enough
    if len(cwd_matches) == 1:
        slug = cwd_matches[0][0]
        return state.tasks[slug], ResolutionSource.CWD
    if len(cwd_matches) >= 2:
        raise AmbiguousTaskError(
            active=sorted(seen_slugs),
            candidates=cwd_matches,
        )

    # 4. No anchor resolved.
    if not state.tasks:
        raise NoActiveTaskError(
            "no active task; run `mship spawn \"description\"` to start one"
        )
    if len(state.tasks) == 1:
        only = next(iter(state.tasks.values()))
        return only, ResolutionSource.SINGLE_ACTIVE
    raise AmbiguousTaskError(
        active=sorted(state.tasks.keys()),
        candidates=[
            (t.slug, _first_worktree_path(t))
            for t in state.tasks.values()
        ],
    )
