import os
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
        import signal

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

        def _kill_group(proc, sig):
            """Send sig to the whole process group. Cross-platform."""
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(proc.pid, sig)
            except (ProcessLookupError, OSError):
                try:
                    proc.send_signal(sig)
                except Exception:
                    pass
            except Exception:
                pass

        if not result.success:
            for repo_result in result.results:
                if not repo_result.success:
                    output.error(f"{repo_result.repo}: failed to start")
            # Terminate any background processes that did start
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGINT)
            raise typer.Exit(code=1)

        if not result.background_processes:
            output.success("All services started")
            return

        # Have background services — wait for them with signal forwarding
        output.success(f"Started {len(result.background_processes)} background service(s). Press Ctrl-C to stop.")

        def _forward_sigint(signum, frame):
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGINT)

        signal.signal(signal.SIGINT, _forward_sigint)

        try:
            for proc in result.background_processes:
                proc.wait()
                # Catch any surviving grandchildren in the process group
                _kill_group(proc, signal.SIGTERM)
            # Brief grace period, then SIGKILL stragglers
            import time
            time.sleep(0.5)
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
        except KeyboardInterrupt:
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGINT)
            for proc in result.background_processes:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    _kill_group(proc, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass

        output.print("All background services have exited")

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
