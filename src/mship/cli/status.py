import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def status():
        """Show current phase, active task, worktrees, and test results."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.print("No active task")
            if not output.is_tty:
                output.json({"current_task": None, "tasks": {}})
            return

        task = state.tasks[state.current_task]
        if output.is_tty:
            output.print(f"[bold]Task:[/bold] {task.slug}")
            phase_str = task.phase
            if task.blocked_reason:
                phase_str = f"{task.phase} (BLOCKED: {task.blocked_reason})"
            output.print(f"[bold]Phase:[/bold] {phase_str}")
            if task.blocked_at:
                output.print(f"[bold]Blocked since:[/bold] {task.blocked_at}")
            output.print(f"[bold]Branch:[/bold] {task.branch}")
            output.print(f"[bold]Repos:[/bold] {', '.join(task.affected_repos)}")
            if task.worktrees:
                output.print("[bold]Worktrees:[/bold]")
                for repo, path in task.worktrees.items():
                    output.print(f"  {repo}: {path}")
            if task.test_results:
                output.print("[bold]Tests:[/bold]")
                for repo, result in task.test_results.items():
                    status_str = (
                        "[green]pass[/green]"
                        if result.status == "pass"
                        else "[red]fail[/red]"
                    )
                    output.print(f"  {repo}: {status_str}")
        else:
            data = task.model_dump(mode="json")
            if task.blocked_reason:
                data["phase_display"] = f"{task.phase} (BLOCKED: {task.blocked_reason})"
            output.json(data)

    @app.command()
    def graph():
        """Show repo dependency graph."""
        container = get_container()
        output = Output()
        config = container.config()
        graph_obj = container.graph()
        order = graph_obj.topo_sort()

        if output.is_tty:
            for repo_name in order:
                repo = config.repos[repo_name]
                deps = repo.depends_on
                dep_str = f" -> [{', '.join(d.repo for d in deps)}]" if deps else ""
                type_str = f"({repo.type})"
                output.print(f"  {repo_name} {type_str}{dep_str}")
        else:
            graph_data = {}
            for name, repo in config.repos.items():
                graph_data[name] = {
                    "type": repo.type,
                    "depends_on": [d.repo for d in repo.depends_on],
                    "path": str(repo.path),
                }
            output.json({"repos": graph_data, "order": order})
