"""CLI glue for mship.core.task_resolver.

Two entry points:

- `resolve_or_exit(state, cli_task)` — returns `Task`. Used by view commands
  that don't need the breadcrumb (`mship status`, `mship logs`, ...).
- `resolve_for_command(cmd, state, cli_task, output)` — returns `ResolvedTask`
  (task + source string). Prints a one-line breadcrumb to stderr when on a
  TTY. Used by state-changing verbs and subagent-feeding commands.

Both catch the three resolver exceptions and raise `typer.Exit(1)` with
friendly messages. When `AmbiguousTaskError.candidates` is populated, both
paths render `--task <slug>  (<worktree path>)` hints.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

import typer

from mship.cli.output import Output
from mship.core.state import Task, WorkspaceState
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
    resolve_task,
)


class ResolvedTask(NamedTuple):
    """Result of `resolve_for_command`.

    `task` is the resolved Task. `source` is the `ResolutionSource.value`
    string (e.g. "cwd", "--task", "MSHIP_TASK", "only active task"),
    suitable for inclusion in JSON payloads.
    """
    task: Task
    source: str


def _format_ambiguity(e: AmbiguousTaskError) -> list[str]:
    """Turn an AmbiguousTaskError into human-readable lines."""
    lines: list[str] = []
    if e.candidates:
        lines.append("Pick one with --task:")
        for slug, path in e.candidates:
            suffix = f"  ({path})" if path else ""
            lines.append(f"  --task {slug}{suffix}")
    else:
        lines.append(
            f"Multiple active tasks ({', '.join(e.active)}). "
            "Specify --task, set MSHIP_TASK, or cd into a worktree."
        )
    return lines


def _handle_resolver_errors(
    state: WorkspaceState, output: Output, fn,
):
    """Shared exception handling for the two resolver entry points."""
    try:
        return fn()
    except NoActiveTaskError as e:
        output.error(str(e))
        raise typer.Exit(1)
    except UnknownTaskError as e:
        known = ", ".join(sorted(state.tasks.keys())) or "(none)"
        output.error(f"Unknown task: {e.slug}. Known: {known}.")
        raise typer.Exit(1)
    except AmbiguousTaskError as e:
        output.error("ambiguous task:")
        for line in _format_ambiguity(e):
            output.error(line)
        raise typer.Exit(1)


def resolve_or_exit(state: WorkspaceState, cli_task: str | None) -> Task:
    output = Output()
    def _go() -> Task:
        task, _source = resolve_task(
            state,
            cli_task=cli_task,
            env_task=os.environ.get("MSHIP_TASK"),
            cwd=Path.cwd(),
        )
        return task
    return _handle_resolver_errors(state, output, _go)


def resolve_for_command(
    cmd_name: str,
    state: WorkspaceState,
    cli_task: str | None,
    output: Output,
) -> ResolvedTask:
    """Resolve a task, print a TTY breadcrumb, return (task, source).

    On non-TTY, the caller is expected to include `resolved_task` and
    `resolution_source` fields in their JSON output (the `source` value
    is exactly what belongs in the JSON).

    `cmd_name` is accepted for forward-compat (future per-command
    suppression, telemetry, richer breadcrumb formatting). Currently unused
    beyond keeping the public signature stable.
    """
    def _go() -> ResolvedTask:
        task, source = resolve_task(
            state,
            cli_task=cli_task,
            env_task=os.environ.get("MSHIP_TASK"),
            cwd=Path.cwd(),
        )
        output.breadcrumb(f"→ task: {task.slug}  (resolved via {source.value})")
        return ResolvedTask(task=task, source=source.value)
    return _handle_resolver_errors(state, output, _go)
