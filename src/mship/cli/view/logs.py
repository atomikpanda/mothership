import os
from pathlib import Path
from typing import Optional

import typer

from mship.cli.view._base import ViewApp
from mship.cli.view._placeholders import placeholder_for
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
    resolve_task,
)


class LogsView(ViewApp):
    def __init__(
        self,
        state_manager,
        log_manager,
        task_slug: Optional[str],
        scope_to_repo: Optional[str] = None,
        *,
        all_: bool = False,
        cli_task: Optional[str] = None,
        cwd: Optional[Path] = None,
        **kw,
    ):
        super().__init__(**kw)
        self._state_manager = state_manager
        self._log_manager = log_manager
        self._task_slug = task_slug
        self._scope_to_repo = scope_to_repo
        self._all = all_
        self._cli_task = cli_task
        self._cwd = cwd if cwd is not None else Path.cwd()

    def _resolve_slug(self) -> str:
        """Return the task slug to render for this tick.

        Non-watch: returns the pre-resolved `task_slug` passed in by the CLI.
        Watch: re-runs `resolve_task()` each call.

        Resolver errors propagate. `gather()` catches `NoActiveTaskError`,
        `AmbiguousTaskError`, and `UnknownTaskError` and renders a placeholder;
        any other exception will bubble up to `ViewApp._refresh_content`'s
        generic error banner.
        """
        if self._task_slug is not None:
            return self._task_slug
        state = self._state_manager.load()
        task, _ = resolve_task(
            state,
            cli_task=self._cli_task,
            env_task=os.environ.get("MSHIP_TASK"),
            cwd=self._cwd,
        )
        return task.slug

    def gather(self) -> str:
        try:
            slug = self._resolve_slug()
        except (NoActiveTaskError, AmbiguousTaskError, UnknownTaskError) as err:
            return placeholder_for(err)

        scope = self._scope_to_repo
        # Watch mode re-reads state per tick so scoping follows `mship switch`.
        # Non-watch trusts the CLI-precomputed `scope_to_repo`. `--all` skips
        # per-tick scoping regardless of mode. The `scope_to_repo is None`
        # guard protects an explicitly-passed scope from being overwritten
        # when this class is constructed outside the CLI (e.g. in tests).
        if (
            self._task_slug is None
            and not self._all
            and self._scope_to_repo is None
        ):
            state = self._state_manager.load()
            task = state.tasks.get(slug)
            if task is not None:
                scope = getattr(task, "active_repo", None)

        entries = self._log_manager.read(slug)
        if scope is not None:
            entries = [e for e in entries if e.repo is None or e.repo == scope]
        if not entries:
            return f"Log for {slug} is empty"
        lines = []
        for entry in entries:
            ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
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
        container = get_container()

        if watch:
            # Watch mode: defer task resolution into the view so resolver
            # errors become placeholder text instead of exit-1.
            task_slug: Optional[str] = None
            cli_task = task
            scope: Optional[str] = None
        else:
            from mship.cli._resolve import resolve_or_exit
            state = container.state_manager().load()
            t = resolve_or_exit(state, task)
            task_slug = t.slug
            cli_task = None
            scope = None
            if not all_ and t.active_repo is not None:
                scope = t.active_repo

        view = LogsView(
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            task_slug=task_slug,
            scope_to_repo=scope,
            all_=all_,
            cli_task=cli_task,
            cwd=Path.cwd(),
            watch=watch,
            interval=interval,
        )
        view.run()
