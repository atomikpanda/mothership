from typing import Optional

import typer

from mship.cli.view._base import ViewApp


class LogsView(ViewApp):
    def __init__(self, state_manager, log_manager, task_slug: Optional[str], scope_to_repo: Optional[str] = None, **kw):
        super().__init__(**kw)
        self._state_manager = state_manager
        self._log_manager = log_manager
        self._task_slug = task_slug
        self._scope_to_repo = scope_to_repo

    def _resolve_slug(self) -> Optional[str]:
        if self._task_slug is not None:
            return self._task_slug
        state = self._state_manager.load()
        return state.current_task

    def gather(self) -> str:
        slug = self._resolve_slug()
        if slug is None:
            return "No active task (and no slug provided)"
        entries = self._log_manager.read(slug)
        if self._scope_to_repo is not None:
            # Keep entries tagged with the target repo OR untagged
            entries = [e for e in entries if e.repo is None or e.repo == self._scope_to_repo]
        if not entries:
            return f"Log for {slug} is empty"
        lines = []
        for entry in entries:
            ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"{ts}  {entry.message}")
        return "\n".join(lines)


def register(app: typer.Typer, get_container):
    @app.command()
    def logs(
        task_slug: Optional[str] = typer.Argument(None, help="Task slug (default: current)"),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        all_: bool = typer.Option(False, "--all", help="Show all log entries, ignore active_repo"),
    ):
        """Live tail of a task's log."""
        container = get_container()
        state = container.state_manager().load()

        if task_slug is not None:
            if not state.tasks:
                typer.echo("No tasks in state.", err=True)
                raise typer.Exit(code=1)
            if task_slug not in state.tasks:
                known = ", ".join(sorted(state.tasks.keys()))
                typer.echo(f"Unknown task '{task_slug}'. Known tasks: {known}.", err=True)
                raise typer.Exit(code=1)

        scope: Optional[str] = None
        if not all_:
            if state.current_task is not None:
                scope = state.tasks[state.current_task].active_repo

        view = LogsView(
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            task_slug=task_slug,
            scope_to_repo=scope,
            watch=watch,
            interval=interval,
        )
        view.run()
