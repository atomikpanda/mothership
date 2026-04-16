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
        # The CLI entry point always resolves the task via resolve_or_exit before
        # constructing this view, so task_slug is expected to be set. Callers
        # that omit it will see the empty-journal path below.
        return self._task_slug

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
            # Structured metadata line (dim, below timestamp)
            meta_parts: list[str] = []
            if entry.repo:
                meta_parts.append(f"repo={entry.repo}")
            if entry.iteration is not None:
                meta_parts.append(f"iter={entry.iteration}")
            if entry.test_state:
                meta_parts.append(f"test={entry.test_state}")
            if entry.action:
                meta_parts.append(f"action={entry.action}")
            meta = f"  [{' '.join(meta_parts)}]" if meta_parts else ""
            lines.append(f"{ts}{meta}")
            lines.append(f"  {entry.message}")
            if entry.open_question:
                lines.append(f"  ⚠ open: {entry.open_question}")
        return "\n".join(lines)


def register(app: typer.Typer, get_container):
    @app.command(name="journal")
    def journal(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var."),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        all_: bool = typer.Option(False, "--all", help="Show all log entries, ignore active_repo"),
    ):
        """Live tail of a task's journal."""
        from mship.cli._resolve import resolve_or_exit

        container = get_container()
        state = container.state_manager().load()

        t = resolve_or_exit(state, task)
        task_slug = t.slug

        scope: Optional[str] = None
        if not all_ and t.active_repo is not None:
            scope = t.active_repo
        view = LogsView(
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            task_slug=task_slug,
            scope_to_repo=scope,
            watch=watch,
            interval=interval,
        )
        view.run()
