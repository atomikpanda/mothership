from pathlib import Path
from typing import Optional

import typer

from mship.cli.view._base import ViewApp
from mship.core.state import Task, WorkspaceState
from mship.core.view.task_index import build_task_index


def _render_task(task: Task) -> str:
    from mship.util.duration import format_relative

    lines = [f"Task:   {task.slug}"]
    if task.finished_at is not None:
        lines.append(
            f"⚠ Finished: {format_relative(task.finished_at)} — run `mship close` after merge"
        )
    if getattr(task, "active_repo", None) is not None:
        lines.append(f"Active repo: {task.active_repo}")
    phase_line = task.phase
    if task.phase_entered_at is not None:
        phase_line = f"{task.phase} (entered {format_relative(task.phase_entered_at)})"
    if task.blocked_reason:
        phase_line += f"  (BLOCKED: {task.blocked_reason})"
    lines.append(f"Phase:  {phase_line}")
    lines.append(f"Branch: {task.branch}")
    lines.append(f"Repos:  {', '.join(task.affected_repos)}")
    if task.worktrees:
        lines.append("Worktrees:")
        for repo, path in task.worktrees.items():
            lines.append(f"  {repo}: {path}")
    if task.test_results:
        lines.append("Tests:")
        for repo, result in task.test_results.items():
            lines.append(f"  {repo}: {result.status}")
    return "\n".join(lines)


class StatusView(ViewApp):
    def __init__(self, state_manager, workspace_root: Path, task_filter: Optional[str], **kw):
        super().__init__(**kw)
        self._state_manager = state_manager
        self._workspace_root = workspace_root
        self._task_filter = task_filter

    def gather(self) -> str:
        state: WorkspaceState = self._state_manager.load()
        if self._task_filter is not None:
            task = state.tasks.get(self._task_filter)
            if task is None:
                return f"Unknown task: {self._task_filter}"
            return _render_task(task)
        index = build_task_index(state, self._workspace_root)
        if not index:
            return "No tasks. Run `mship spawn \"…\"` to start one."
        blocks = [_render_task(state.tasks[s.slug]) for s in index]
        return "\n\n─────────────\n\n".join(blocks)


def register(app: typer.Typer, get_container):
    @app.command()
    def status(
        task: Optional[str] = typer.Option(None, "--task", help="Narrow to one task slug"),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
    ):
        """Live workspace status view (all tasks by default)."""
        from pathlib import Path as _P
        container = get_container()
        if task is not None:
            state = container.state_manager().load()
            if task not in state.tasks:
                known = ", ".join(sorted(state.tasks.keys())) or "(none)"
                typer.echo(f"Unknown task '{task}'. Known: {known}.", err=True)
                raise typer.Exit(code=1)
        workspace_root = _P(container.config_path()).parent
        view = StatusView(
            state_manager=container.state_manager(),
            workspace_root=workspace_root,
            task_filter=task,
            watch=watch,
            interval=interval,
        )
        view.run()
