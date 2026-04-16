from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def status(
        task: Optional[str] = typer.Option(
            None, "--task", help="Target task (default: cwd/env)"
        ),
    ):
        """Show status of a task (resolved from cwd/env/flag) or workspace summary."""
        from datetime import datetime, timezone
        from mship.util.duration import format_relative
        from mship.cli._resolve import resolve_or_exit
        from mship.core.task_resolver import (
            AmbiguousTaskError, NoActiveTaskError, resolve_task,
        )
        import os
        from pathlib import Path

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        t = None
        if task is not None or os.environ.get("MSHIP_TASK"):
            # Explicit target — resolve_or_exit shows friendly error on miss.
            t = resolve_or_exit(state, task)
        else:
            try:
                t = resolve_task(
                    state, cli_task=None, env_task=None, cwd=Path.cwd(),
                )
            except (NoActiveTaskError, AmbiguousTaskError):
                t = None

        if t is None:
            active = sorted(
                state.tasks.values(),
                key=lambda tt: (tt.phase_entered_at or tt.created_at),
                reverse=True,
            )
            if output.is_tty:
                if not active:
                    output.print("No active tasks. Run `mship spawn \"description\"`.")
                else:
                    output.print(f"[bold]Active tasks ({len(active)}):[/bold]")
                    for tt in active:
                        phase_rel = (
                            format_relative(tt.phase_entered_at)
                            if tt.phase_entered_at else "—"
                        )
                        output.print(
                            f"  {tt.slug}  "
                            f"phase={tt.phase} (entered {phase_rel})  "
                            f"branch={tt.branch}"
                        )
            else:
                output.json({
                    "active_tasks": [
                        {
                            "slug": tt.slug,
                            "phase": tt.phase,
                            "branch": tt.branch,
                            "phase_entered_at": (
                                tt.phase_entered_at.isoformat()
                                if tt.phase_entered_at else None
                            ),
                        }
                        for tt in active
                    ],
                })
            return

        # Single-task detail — `t` is the resolved task from resolve_or_exit.
        task_obj = t

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
                config, shell, names=task_obj.affected_repos,
                known_worktree_paths=known, local_only=True,
            )
            errors = [i for r in report.repos for i in r.issues if i.severity == "error"]
            drift_summary = {"has_errors": bool(errors), "error_count": len(errors)}
        except Exception:
            pass

        last_log: dict | None = None
        try:
            entries = container.log_manager().read(task_obj.slug, last=1)
            if entries:
                e = entries[-1]
                first_line = e.message.splitlines()[0] if e.message else ""
                last_log = {"message": first_line[:60], "timestamp": e.timestamp}
        except Exception:
            last_log = None

        if output.is_tty:
            output.print(f"[bold]Task:[/bold] {task_obj.slug}")
            if task_obj.finished_at is not None:
                output.print(
                    f"[yellow]⚠ Finished:[/yellow] {format_relative(task_obj.finished_at)} — run `mship close` after merge"
                )
            if task_obj.active_repo is not None:
                output.print(f"[bold]Active repo:[/bold] {task_obj.active_repo}")
            phase_str = task_obj.phase
            if task_obj.phase_entered_at is not None:
                rel = format_relative(task_obj.phase_entered_at)
                phase_str = f"{task_obj.phase} (entered {rel})"
            if task_obj.blocked_reason:
                phase_str = f"{phase_str}  [red]BLOCKED:[/red] {task_obj.blocked_reason}"
            output.print(f"[bold]Phase:[/bold] {phase_str}")
            if task_obj.blocked_at:
                output.print(f"[bold]Blocked since:[/bold] {task_obj.blocked_at}")
            output.print(f"[bold]Branch:[/bold] {task_obj.branch}")
            output.print(f"[bold]Repos:[/bold] {', '.join(task_obj.affected_repos)}")
            if task_obj.worktrees:
                output.print("[bold]Worktrees:[/bold]")
                for repo, path in task_obj.worktrees.items():
                    output.print(f"  {repo}: {path}")
            if task_obj.test_results:
                output.print("[bold]Tests:[/bold]")
                for repo, result in task_obj.test_results.items():
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
            data = task_obj.model_dump(mode="json")
            data["active_repo"] = task_obj.active_repo
            if task_obj.blocked_reason:
                data["phase_display"] = f"{task_obj.phase} (BLOCKED: {task_obj.blocked_reason})"
            if task_obj.finished_at is not None:
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
