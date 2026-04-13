import typer

from mship.cli.view._base import ViewApp


class StatusView(ViewApp):
    def __init__(self, state_manager, **kw):
        super().__init__(**kw)
        self._state_manager = state_manager

    def gather(self) -> str:
        from mship.util.duration import format_relative

        state = self._state_manager.load()
        if state.current_task is None:
            return "No active task"
        task = state.tasks[state.current_task]

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


def register(app: typer.Typer, get_container):
    @app.command()
    def status(
        watch: bool = typer.Option(False, "--watch", help="Refresh on interval"),
        interval: float = typer.Option(2.0, "--interval", help="Refresh seconds"),
    ):
        """Live workspace status view."""
        container = get_container()
        view = StatusView(
            state_manager=container.state_manager(),
            watch=watch,
            interval=interval,
        )
        view.run()
