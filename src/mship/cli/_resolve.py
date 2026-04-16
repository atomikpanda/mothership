"""CLI glue for mship.core.task_resolver.

Catches the three resolver exceptions, writes a friendly error to stderr,
and raises typer.Exit(1). Usage:

    t = resolve_or_exit(state, cli_task)  # `cli_task` comes from --task option
"""
from __future__ import annotations

import os
from pathlib import Path

import typer

from mship.cli.output import Output
from mship.core.state import Task, WorkspaceState
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
    resolve_task,
)


def resolve_or_exit(state: WorkspaceState, cli_task: str | None) -> Task:
    output = Output()
    try:
        return resolve_task(
            state,
            cli_task=cli_task,
            env_task=os.environ.get("MSHIP_TASK"),
            cwd=Path.cwd(),
        )
    except NoActiveTaskError as e:
        output.error(str(e))
        raise typer.Exit(1)
    except UnknownTaskError as e:
        known = ", ".join(sorted(state.tasks.keys())) or "(none)"
        output.error(f"Unknown task: {e.slug}. Known: {known}.")
        raise typer.Exit(1)
    except AmbiguousTaskError as e:
        output.error(
            f"Multiple active tasks ({', '.join(e.active)}). "
            "Specify --task, set MSHIP_TASK, or cd into a worktree."
        )
        raise typer.Exit(1)
