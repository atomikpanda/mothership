import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def status():
        """Show current phase, active task, worktrees, test results, drift, and recent activity."""
        from datetime import datetime, timezone
        from mship.util.duration import format_relative

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

        # Drift (local-only)
        drift_summary: dict = {"has_errors": False, "error_count": 0}
        try:
            from mship.core.repo_state import audit_repos
            from mship.core.audit_gate import collect_known_worktree_paths
            config = container.config()
            shell = container.shell()
            try:
                known = collect_known_worktree_paths(state_mgr)
            except Exception:
                known = frozenset()
            report = audit_repos(
                config, shell, names=task.affected_repos,
                known_worktree_paths=known, local_only=True,
            )
            errors = [i for r in report.repos for i in r.issues if i.severity == "error"]
            drift_summary = {"has_errors": bool(errors), "error_count": len(errors)}
        except Exception:
            pass  # drift line simply omitted on failure

        # Last log
        last_log: dict | None = None
        try:
            entries = container.log_manager().read(task.slug, last=1)
            if entries:
                e = entries[-1]
                first_line = e.message.splitlines()[0] if e.message else ""
                last_log = {"message": first_line[:60], "timestamp": e.timestamp}
        except Exception:
            last_log = None

        if output.is_tty:
            output.print(f"[bold]Task:[/bold] {task.slug}")
            if task.finished_at is not None:
                output.print(
                    f"[yellow]⚠ Finished:[/yellow] {format_relative(task.finished_at)} — run `mship close` after merge"
                )
            phase_str = task.phase
            if task.phase_entered_at is not None:
                rel = format_relative(task.phase_entered_at)
                phase_str = f"{task.phase} (entered {rel})"
            if task.blocked_reason:
                phase_str = f"{phase_str}  [red]BLOCKED:[/red] {task.blocked_reason}"
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
                        "[green]pass[/green]" if result.status == "pass"
                        else "[red]fail[/red]"
                    )
                    output.print(f"  {repo}: {status_str}")
            if drift_summary["has_errors"]:
                output.print(
                    f"[bold]Drift:[/bold] [red]{drift_summary['error_count']} error(s)[/red] — run `mship audit`"
                )
            else:
                output.print("[bold]Drift:[/bold] [green]clean[/green]")
            if last_log is not None:
                ts_rel = format_relative(last_log["timestamp"])
                output.print(f"[bold]Last log:[/bold] \"{last_log['message']}\" ({ts_rel})")
        else:
            data = task.model_dump(mode="json")
            if task.blocked_reason:
                data["phase_display"] = f"{task.phase} (BLOCKED: {task.blocked_reason})"
            if task.finished_at is not None:
                data["close_hint"] = "mship close"
            data["drift"] = drift_summary
            data["last_log"] = (
                {"message": last_log["message"], "timestamp": last_log["timestamp"].isoformat()}
                if last_log is not None else None
            )
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
