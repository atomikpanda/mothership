from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(name="log")
    def log_cmd(
        message: Optional[str] = typer.Argument(None, help="Message to append to task log"),
        last: Optional[int] = typer.Option(None, "--last", help="Show only last N entries"),
    ):
        """Append to or read the current task's log."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn \"description\"` to start one.")
            raise typer.Exit(code=1)

        log_mgr = container.log_manager()

        if message is not None:
            log_mgr.append(state.current_task, message)
            if output.is_tty:
                output.success("Logged")
            else:
                output.json({"task": state.current_task, "logged": message})
        else:
            entries = log_mgr.read(state.current_task, last=last)
            if not entries:
                output.print("No log entries")
                return
            if output.is_tty:
                for entry in entries:
                    output.print(f"[dim]{entry.timestamp.strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
                    output.print(f"  {entry.message}")
            else:
                output.json({
                    "task": state.current_task,
                    "entries": [
                        {"timestamp": e.timestamp.isoformat(), "message": e.message}
                        for e in entries
                    ],
                })
