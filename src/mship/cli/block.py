from datetime import datetime, timezone

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def block(reason: str):
        """Mark the current task as blocked."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn \"description\"` to start one.")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        task.blocked_reason = reason
        task.blocked_at = datetime.now(timezone.utc)
        state_mgr.save(state)

        log_mgr = container.log_manager()
        log_mgr.append(state.current_task, f"Blocked: {reason}")

        if output.is_tty:
            output.success(f"Task blocked: {reason}")
        else:
            output.json({"task": state.current_task, "blocked_reason": reason})

    @app.command()
    def unblock():
        """Clear the blocked state on the current task."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn \"description\"` to start one.")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        if task.blocked_reason is None:
            output.error("Task is not blocked. Use `mship block \"reason\"` to mark it as blocked.")
            raise typer.Exit(code=1)

        task.blocked_reason = None
        task.blocked_at = None
        state_mgr.save(state)

        log_mgr = container.log_manager()
        log_mgr.append(state.current_task, "Unblocked")

        if output.is_tty:
            output.success("Task unblocked")
        else:
            output.json({"task": state.current_task, "blocked_reason": None})
