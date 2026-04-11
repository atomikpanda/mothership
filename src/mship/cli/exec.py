from typing import Optional

import typer

from mship.cli.output import Output


def _resolve_repos(
    config, task_affected: list[str],
    repos_filter: str | None, tag_filter: list[str] | None,
) -> list[str]:
    """Resolve target repos from --repos and --tag filters."""
    candidates = None

    if repos_filter:
        candidates = set(repos_filter.split(","))
        for name in candidates:
            if name not in config.repos:
                raise ValueError(f"Unknown repo: {name}")

    if tag_filter:
        tagged = set()
        for name, repo in config.repos.items():
            if any(t in repo.tags for t in tag_filter):
                tagged.add(name)
        if candidates is not None:
            candidates = candidates & tagged
        else:
            candidates = tagged

    if candidates is not None:
        return list(candidates)
    return task_affected


def register(app: typer.Typer, get_container):
    @app.command(name="test")
    def test_cmd(
        run_all: bool = typer.Option(False, "--all", help="Run all repos even on failure"),
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names to filter"),
        tag: Optional[list[str]] = typer.Option(None, "--tag", help="Filter repos by tag"),
    ):
        """Run tests across affected repos in dependency order."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn \"description\"` to start one.")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        config = container.config()

        try:
            target_repos = _resolve_repos(config, task.affected_repos, repos, tag)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        executor = container.executor()
        result = executor.execute(
            "test",
            repos=target_repos,
            run_all=run_all,
            task_slug=state.current_task,
        )

        for repo_result in result.results:
            if repo_result.success:
                output.success(f"{repo_result.repo}: pass")
            else:
                output.error(f"{repo_result.repo}: fail")
                if repo_result.shell_result.stderr:
                    output.print(repo_result.shell_result.stderr.strip())

        if not result.success:
            raise typer.Exit(code=1)

    @app.command(name="run")
    def run_cmd(
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names to filter"),
        tag: Optional[list[str]] = typer.Option(None, "--tag", help="Filter repos by tag"),
    ):
        """Start services across repos in dependency order."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn \"description\"` to start one.")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        config = container.config()

        try:
            target_repos = _resolve_repos(config, task.affected_repos, repos, tag)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        executor = container.executor()
        result = executor.execute("run", repos=target_repos)

        if not result.success:
            for repo_result in result.results:
                if not repo_result.success:
                    output.error(f"{repo_result.repo}: failed to start")
            raise typer.Exit(code=1)
        output.success("All services started")

    @app.command()
    def logs(
        service: str,
    ):
        """Tail logs for a specific service."""
        container = get_container()
        output = Output()
        config = container.config()

        if service not in config.repos:
            output.error(f"Unknown service: {service}")
            raise typer.Exit(code=1)

        repo = config.repos[service]
        shell = container.shell()
        actual_task = repo.tasks.get("logs", "logs")
        env_runner = repo.env_runner or config.env_runner

        # Use worktree path if available
        from pathlib import Path
        cwd = repo.path
        state_mgr = container.state_manager()
        state = state_mgr.load()
        if state.current_task:
            task = state.tasks.get(state.current_task)
            if task and service in task.worktrees:
                wt_path = Path(task.worktrees[service])
                if wt_path.exists():
                    cwd = wt_path

        result = shell.run_task(
            task_name="logs",
            actual_task_name=actual_task,
            cwd=cwd,
            env_runner=env_runner,
        )
        output.print(result.stdout)
