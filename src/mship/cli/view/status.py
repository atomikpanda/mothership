import typer

from mship.cli.view._base import ViewApp


class StatusView(ViewApp):
    def __init__(self, state_manager, **kw):
        super().__init__(**kw)
        self._state_manager = state_manager

    def gather(self) -> str:
        state = self._state_manager.load()
        if state.current_task is None:
            return "No active task"
        task = state.tasks[state.current_task]
        lines = [
            f"Task:   {task.slug}",
            f"Phase:  {task.phase}"
            + (f"  (BLOCKED: {task.blocked_reason})" if task.blocked_reason else ""),
            f"Branch: {task.branch}",
            f"Repos:  {', '.join(task.affected_repos)}",
        ]
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
