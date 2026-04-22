from datetime import datetime, timezone
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def block(
        reason: str,
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var."),
    ):
        """Mark a task as blocked."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        resolved = resolve_for_command("block", state, task, output)
        t = resolved.task

        if t.blocked_reason is not None:
            output.error(
                f"Task is already blocked: {t.blocked_reason}. "
                "Run `mship unblock` first to clear the existing block."
            )
            raise typer.Exit(code=1)

        def _apply(s):
            s.tasks[t.slug].blocked_reason = reason
            s.tasks[t.slug].blocked_at = datetime.now(timezone.utc)

        state_mgr.mutate(_apply)

        log_mgr = container.log_manager()
        log_mgr.append(t.slug, f"Blocked: {reason}")

        if output.is_tty:
            output.success(f"Task blocked: {reason}")
        else:
            output.json({
                "task": t.slug,
                "blocked_reason": reason,
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })

    @app.command()
    def unblock(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var."),
    ):
        """Clear the blocked state on a task."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        resolved = resolve_for_command("unblock", state, task, output)
        t = resolved.task

        if t.blocked_reason is None:
            output.error("Task is not blocked. Use `mship block \"reason\"` to mark it as blocked.")
            raise typer.Exit(code=1)

        def _apply(s):
            s.tasks[t.slug].blocked_reason = None
            s.tasks[t.slug].blocked_at = None

        state_mgr.mutate(_apply)

        log_mgr = container.log_manager()
        log_mgr.append(t.slug, "Unblocked")

        if output.is_tty:
            output.success("Task unblocked")
        else:
            output.json({
                "task": t.slug,
                "blocked_reason": None,
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })
