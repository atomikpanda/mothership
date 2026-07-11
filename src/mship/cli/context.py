"""`mship context` — emit a one-shot agent-readable JSON snapshot of workspace state.

See GitHub issue #50. Always emits JSON to stdout (the load-bearing surface for
agents); a `--human` formatter can be added later without breaking the schema.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

import os

from mship.cli.output import Output
from mship.core.context import AudienceError, build_context
from mship.core.reconcile.cache import ReconcileCache
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
    resolve_task,
)


def _render_audience_block(audience: dict) -> None:
    """Print a readable rendition of `audience["instructions"]` (MOS-100
    ac9). TTY-only -- callers must guard with `output.human_mode` first, same
    as the existing `mship spec show` human-render path (cli/spec.py)."""
    from rich.console import Console
    from rich.markdown import Markdown

    kind_suffix = f" ({audience['kind']})" if audience.get("kind") else ""
    Console().print(Markdown(
        f"**Audience: {audience['for']}{kind_suffix}**\n\n{audience['instructions']}"
    ))


def register(app: typer.Typer, get_container):
    @app.command()
    def context(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var."),
        for_: Optional[str] = typer.Option(
            None, "--for",
            help="Shape the output for a specific audience: claude-code | codex | human | reviewer.",
        ),
        kind: Optional[str] = typer.Option(
            None, "--kind",
            help="Reviewer sub-kind (only valid with --for reviewer): spec | code-quality.",
        ),
    ):
        """Emit a JSON snapshot of workspace state for agent consumption."""
        container = get_container()
        output = Output()
        state_dir = container.state_dir()
        state = container.state_manager().load()
        try:
            payload = build_context(
                state=state,
                config=container.config(),
                log_manager=container.log_manager(),
                cwd=Path.cwd(),
                state_dir=state_dir,
                cache=ReconcileCache(state_dir),
                for_=for_,
                kind=kind,
            )
        except AudienceError as e:
            output.error(str(e))
            raise typer.Exit(code=2)
        payload["docs_dir"] = container.config().docs_dir
        try:
            resolved_task, source = resolve_task(
                state,
                cli_task=task,
                env_task=os.environ.get("MSHIP_TASK"),
                cwd=Path.cwd(),
            )
            output.breadcrumb(f"→ task: {resolved_task.slug}  (resolved via {source.value})")
            payload["resolved_task"] = resolved_task.slug
            payload["resolution_source"] = source.value
        except (NoActiveTaskError, AmbiguousTaskError, UnknownTaskError):
            pass

        audience = payload.get("audience")
        if output.human_mode and audience is not None:
            _render_audience_block(audience)

        output.json(payload)
