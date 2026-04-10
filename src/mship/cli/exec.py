from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(name="test")
    def test_cmd(
        run_all: bool = typer.Option(False, "--all", help="Run all repos even on failure"),
    ):
        """Run tests across affected repos in dependency order."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        executor = container.executor()
        result = executor.execute(
            "test",
            repos=task.affected_repos,
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
    def run_cmd():
        """Start services across repos in dependency order."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        executor = container.executor()
        result = executor.execute("run", repos=task.affected_repos)

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

        result = shell.run_task(
            task_name="logs",
            actual_task_name=actual_task,
            cwd=repo.path,
            env_runner=env_runner,
        )
        output.print(result.stdout)
