"""`mship depends` — manage task-to-task dependency edges (#104)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output
from mship.core.state import DependencyEdge
from mship.core.task_graph import find_cycle, downstream_of


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

    @depends_app.command("list")
    def list_cmd(
        task: Optional[str] = typer.Option(None, "--task", help="Task slug to scope to."),
        graph: bool = typer.Option(False, "--graph", help="Emit the full workspace DAG instead of one task's edges."),
    ):
        """List dependencies for a task (default) or the whole workspace (--graph)."""
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
