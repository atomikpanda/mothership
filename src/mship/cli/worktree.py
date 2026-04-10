from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def spawn(
        description: str,
        repos: Optional[str] = typer.Option(None, help="Comma-separated repo names"),
    ):
        """Create coordinated worktrees across repos for a new task."""
        container = get_container()
        output = Output()
        wt_mgr = container.worktree_manager()

        repo_list = repos.split(",") if repos else None

        task = wt_mgr.spawn(description, repos=repo_list)

        if output.is_tty:
            output.success(f"Spawned task: {task.slug}")
            output.print(f"  Branch: {task.branch}")
            output.print(f"  Phase: {task.phase}")
            output.print(f"  Repos: {', '.join(task.affected_repos)}")
            for repo, path in task.worktrees.items():
                output.print(f"  {repo}: {path}")
        else:
            output.json(task.model_dump(mode="json"))

    @app.command()
    def worktrees():
        """List active worktrees grouped by task."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if not state.tasks:
            output.print("No active worktrees")
            return

        if output.is_tty:
            for slug, task in state.tasks.items():
                active = " (active)" if slug == state.current_task else ""
                output.print(f"[bold]{slug}[/bold]{active} [{task.phase}]")
                output.print(f"  Branch: {task.branch}")
                for repo, path in task.worktrees.items():
                    output.print(f"  {repo}: {path}")
        else:
            data = {
                slug: task.model_dump(mode="json")
                for slug, task in state.tasks.items()
            }
            output.json({"current_task": state.current_task, "tasks": data})

    @app.command()
    def abort(
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    ):
        """Discard worktrees and abandon the current task."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task to abort")
            raise typer.Exit(code=1)

        task_slug = state.current_task

        if not yes and output.is_tty:
            from InquirerPy import inquirer

            confirm = inquirer.confirm(
                message=f"Abort task '{task_slug}'? This will remove all worktrees.",
                default=False,
            ).execute()
            if not confirm:
                output.print("Aborted")
                raise typer.Exit(code=0)

        wt_mgr = container.worktree_manager()
        wt_mgr.abort(task_slug)
        output.success(f"Aborted task: {task_slug}")

    @app.command()
    def finish(
        handoff: bool = typer.Option(False, "--handoff", help="Generate CI handoff manifest"),
    ):
        """Create PRs and clean up worktrees in dependency order."""
        from pathlib import Path

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task to finish")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        graph = container.graph()
        config = container.config()
        ordered = graph.topo_sort(task.affected_repos)

        if handoff:
            from mship.core.handoff import generate_handoff

            state_dir = container.state_dir()
            repo_paths = {name: config.repos[name].path for name in ordered}
            repo_deps = {name: config.repos[name].depends_on for name in ordered}
            path = generate_handoff(
                handoffs_dir=Path(state_dir) / "handoffs",
                task_slug=task.slug,
                branch=task.branch,
                ordered_repos=ordered,
                repo_paths=repo_paths,
                repo_deps=repo_deps,
            )
            if output.is_tty:
                output.success(f"Handoff manifest written to: {path}")
            else:
                output.json({"handoff": str(path), "task": task.slug})
            return

        if output.is_tty:
            output.print(f"[bold]Finishing task:[/bold] {task.slug}")
            output.print(f"[bold]Merge order:[/bold]")
            for i, repo in enumerate(ordered, 1):
                output.print(f"  {i}. {repo}")
        else:
            output.json({
                "task": task.slug,
                "merge_order": ordered,
                "status": "manual_pr_required",
            })

        output.warning(
            "PR creation not yet implemented in v1. "
            "Use `gh pr create` manually in each repo in the order shown above."
        )
