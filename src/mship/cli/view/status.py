from pathlib import Path
from typing import Optional

import typer

from mship.cli.view._base import ViewApp
from mship.core.state import Task, WorkspaceState
from mship.core.view.task_index import build_task_index
from mship.core.view.status_grouping import WorkItemTaskGroup, group_tasks_by_workitem


def _render_group_header(group: WorkItemTaskGroup) -> str:
    title = group.title or "(untitled)"
    return f"◆ {group.work_item_id}  ·  {title}  ·  [{group.phase}]"


def _render_task(task: Task, open_questions: list[str] | None = None) -> str:
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
        iter_suffix = f"  (iteration {task.test_iteration})" if getattr(task, "test_iteration", 0) else ""
        lines.append(f"Tests:{iter_suffix}")
        for repo, result in task.test_results.items():
            lines.append(f"  {repo}: {result.status}")
    elif getattr(task, "test_iteration", 0):
        lines.append(f"Tests: (iteration {task.test_iteration}, no results stored)")
    if open_questions:
        lines.append(f"Open questions: {len(open_questions)}")
        for q in open_questions[:3]:  # show at most 3 in the summary
            lines.append(f"  ⚠ {q}")
        if len(open_questions) > 3:
            lines.append(f"  … and {len(open_questions) - 3} more")
    return "\n".join(lines)


class StatusView(ViewApp):
    def __init__(self, state_manager, workspace_root: Path, task_filter: Optional[str],
                 log_manager=None, workitem_loader=None, task_provider=None, **kw):
        super().__init__(**kw)
        self._state_manager = state_manager
        self._workspace_root = workspace_root
        self._task_filter = task_filter
        self._log_manager = log_manager
        self._workitem_loader = workitem_loader
        self._task_provider = task_provider

    def _open_questions(self, slug: str) -> list[str]:
        if self._log_manager is None:
            return []
        try:
            entries = self._log_manager.read(slug)
            return [e.open_question for e in entries if e.open_question]
        except Exception:
            return []

    def gather(self) -> str:
        state: WorkspaceState = self._state_manager.load()
        task_filter = self._task_filter
        if self._task_provider is not None:
            task_filter = self._task_provider()
            if task_filter is None:
                from mship.cli.view._follow import follow_hint
                return follow_hint()
        if task_filter is not None:
            task = state.tasks.get(task_filter)
            if task is None:
                return f"Unknown task: {task_filter}"
            return _render_task(task, self._open_questions(task.slug))
        index = build_task_index(state, self._workspace_root)
        if not index:
            return "No tasks. Run `mship spawn \"…\"` to start one."
        workitems = self._workitem_loader() if self._workitem_loader is not None else []
        groups = group_tasks_by_workitem(index, workitems)
        blocks: list[str] = []
        for g in groups:
            body = "\n\n─────────────\n\n".join(
                _render_task(state.tasks[t.slug], self._open_questions(t.slug)) for t in g.tasks
            )
            blocks.append(f"{_render_group_header(g)}\n\n{body}" if g.work_item_id is not None else body)
        return "\n\n═════════════\n\n".join(blocks)


def register(app: typer.Typer, get_container):
    @app.command()
    def status(
        task: Optional[str] = typer.Option(None, "--task", help="Narrow to one task slug"),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        follow: bool = typer.Option(False, "--follow", help="Track the workspace CURRENT FOCUS (cockpit-v2)."),
    ):
        """Live workspace status view (all tasks by default)."""
        from pathlib import Path as _P
        from mship.cli._resolve import resolve_or_exit
        from mship.cli.view._workitems import load_workitem_index

        container = get_container()
        workspace_root = _P(container.config_path()).parent

        if follow:
            if task is not None:
                typer.echo("Error: --follow and --task are mutually exclusive.", err=True)
                raise typer.Exit(code=1)
            from mship.cli.view._follow import read_focused_id
            from mship.cli.layout import resolve_focus_target
            from mship.cli.output import Output

            def _task_provider() -> Optional[str]:
                item_id = read_focused_id(container)
                if item_id is None:
                    return None
                target = resolve_focus_target(container, item_id)
                return target[1] if target is not None else None

            view = StatusView(
                state_manager=container.state_manager(),
                workspace_root=workspace_root,
                task_filter=None,
                log_manager=container.log_manager(),
                workitem_loader=lambda: load_workitem_index(container),
                task_provider=_task_provider,
                watch=True,
                interval=interval,
            )
            if not Output().is_tty:
                typer.echo(view.gather())
                return
            view.run()
            return

        task_slug: Optional[str] = None
        if task is not None:
            state = container.state_manager().load()
            t = resolve_or_exit(state, task)
            task_slug = t.slug
        view = StatusView(
            state_manager=container.state_manager(),
            workspace_root=workspace_root,
            task_filter=task_slug,
            log_manager=container.log_manager(),
            workitem_loader=lambda: load_workitem_index(container),
            watch=watch,
            interval=interval,
        )
        view.run()
