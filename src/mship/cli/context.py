"""`mship context` — emit a one-shot agent-readable JSON snapshot of workspace state.

See GitHub issue #50. Always emits JSON to stdout (the load-bearing surface for
agents); a `--human` formatter can be added later without breaking the schema.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output
from mship.core.context import build_context
from mship.core.reconcile.cache import ReconcileCache


def register(app: typer.Typer, get_container):
    @app.command()
    def context(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var."),
    ):
        """Emit a JSON snapshot of workspace state for agent consumption."""
        container = get_container()
        output = Output()
        state_dir = container.state_dir()
        state = container.state_manager().load()
        payload = build_context(
            state=state,
            config=container.config(),
            log_manager=container.log_manager(),
            cwd=Path.cwd(),
            state_dir=state_dir,
            cache=ReconcileCache(state_dir),
        )
        if task is not None:
            resolved = resolve_for_command("context", state, task, output)
            payload["resolved_task"] = resolved.task.slug
            payload["resolution_source"] = resolved.source
        output.json(payload)
