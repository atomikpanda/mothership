from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(rich_help_panel="Workflow")
    def heartbeat(
        task: Optional[str] = typer.Option(
            None, "--task",
            help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var.",
        ),
    ):
        """Stamp a task's activity heartbeat (last_activity_at). No other side effects."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        resolved = resolve_for_command("heartbeat", state, task, output)
        t = resolved.task

        state_mgr.record_activity(t.slug)
        stamped = state_mgr.load().tasks[t.slug].last_activity_at

        if output.human_mode:
            output.success(f"Heartbeat: {t.slug}")
        else:
            output.json({
                "task": t.slug,
                "last_activity_at": stamped.isoformat() if stamped else None,
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })
