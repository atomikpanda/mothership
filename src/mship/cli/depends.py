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
