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
    @app.command(name="journal")
    def journal(
        task: Optional[str] = typer.Option(None, "--task", help="Task slug (default: picker / current)"),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        all_: bool = typer.Option(False, "--all", help="Show all log entries, ignore active_repo"),
    ):
        """Live tail of a task's journal (picker when no task specified)."""
        from pathlib import Path as _P

        container = get_container()
        state = container.state_manager().load()

        task_slug = task
        if task_slug is None and state.current_task is not None:
            task_slug = state.current_task

        if task_slug is not None:
            if task_slug not in state.tasks:
                known = ", ".join(sorted(state.tasks.keys())) or "(none)"
                typer.echo(f"Unknown task '{task_slug}'. Known: {known}.", err=True)
                raise typer.Exit(code=1)
            scope: Optional[str] = None
            if not all_ and state.current_task == task_slug:
                scope = state.tasks[task_slug].active_repo
            view = LogsView(
                state_manager=container.state_manager(),
                log_manager=container.log_manager(),
                task_slug=task_slug,
                scope_to_repo=scope,
                watch=watch,
                interval=interval,
            )
            view.run()
            return

        # No task + no current: show picker.
        from mship.cli.view._picker import TaskPicker, picker_rows
        from mship.core.view.task_index import build_task_index

        workspace_root = _P(container.config_path()).parent
        index = build_task_index(state, workspace_root)
        selected: dict[str, str] = {}
        def _on_select(slug: str) -> None:
            selected["slug"] = slug
        picker = TaskPicker(
            rows=picker_rows(index), on_select=_on_select, watch=False, interval=interval,
        )
        picker.run()
        chosen = selected.get("slug")
        if chosen is None:
            return
        view = LogsView(
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            task_slug=chosen,
            scope_to_repo=None,
            watch=watch,
            interval=interval,
        )
        view.run()
