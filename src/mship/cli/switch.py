from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def switch(
        repo: Optional[str] = typer.Argument(None, help="Repo to switch to. Omit to re-render current."),
    ):
        """Switch active repo within the current task; emit an orientation handoff."""
        from pathlib import Path

        from mship.core.switch import build_handoff
        from mship.util.duration import format_relative

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()
        config = container.config()
        shell = container.shell()
        log_mgr = container.log_manager()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn` to start one.")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]

        if repo is None:
            if task.active_repo is None:
                output.error("No active repo. Run `mship switch <repo>` first.")
                raise typer.Exit(code=1)
            target = task.active_repo
            is_switch = False
        else:
            if repo not in task.affected_repos:
                valid = ", ".join(task.affected_repos)
                output.error(f"Unknown repo '{repo}'. Valid: {valid}.")
                raise typer.Exit(code=1)
            target = repo
            is_switch = True

            # Snapshot every dep's current HEAD SHA before rendering.
            snapshot: dict[str, str] = {}
            repo_cfg = config.repos[target]
            for dep in repo_cfg.depends_on:
                dep_name = dep.repo
                dep_wt = task.worktrees.get(dep_name)
                if dep_wt is None or not Path(dep_wt).exists():
                    continue
                result = shell.run("git rev-parse HEAD", cwd=Path(dep_wt))
                if result.returncode == 0 and result.stdout.strip():
                    snapshot[dep_name] = result.stdout.strip()

            task.active_repo = target
            task.last_switched_at_sha[target] = snapshot
            state_mgr.save(state)

        handoff = build_handoff(config, state_mgr.load(), shell, log_mgr, repo=target)

        if not output.is_tty:
            output.json(handoff.to_json())
            return

        # TTY rendering
        verb = "Switched to" if is_switch else "Currently at"
        lines: list[str] = []

        # Prepend the cd hint when cwd is not inside the worktree.
        from pathlib import Path as _P
        try:
            cwd_r = _P.cwd().resolve()
            wt_r = handoff.worktree_path.resolve()
            cwd_inside = False
            try:
                cwd_r.relative_to(wt_r)
                cwd_inside = True
            except ValueError:
                cwd_inside = False
        except (OSError, RuntimeError):
            cwd_inside = True  # can't determine → don't nag
        if not cwd_inside and not handoff.worktree_missing:
            lines.append(f"[bold red]\u26a0 cd {handoff.worktree_path}[/bold red]")
            lines.append("")

        if handoff.worktree_missing:
            lines.append(
                f"[red]\u26a0 worktree missing:[/red] {handoff.worktree_path} "
                f"(run `mship prune` or `mship close`)"
            )
        if handoff.finished_at is not None:
            lines.append(
                f"[yellow]\u26a0 task finished {format_relative(handoff.finished_at)}[/yellow] "
                f"\u2014 run `mship close` after merge"
            )
        lines.append(
            f"[bold]{verb}:[/bold] {handoff.repo} (task: {handoff.task_slug}, phase: {handoff.phase})"
        )
        lines.append(f"[bold]Branch:[/bold]   {handoff.branch}")
        lines.append(f"[bold]Worktree:[/bold] {handoff.worktree_path}")
        lines.append("")

        if handoff.dep_changes:
            lines.append("[bold]Dependencies changed since your last switch here:[/bold]")
            for d in handoff.dep_changes:
                if d.error is not None:
                    lines.append(f"  [red]{d.repo}: {d.error}[/red]")
                    continue
                lines.append(f"  [green]{d.repo}[/green] ({d.commit_count} commits):")
                for c in d.commits:
                    lines.append(f"    {c}")
                files_str = ", ".join(d.files_changed) if d.files_changed else "(no files)"
                lines.append(
                    f"    files:   {files_str}  (+{d.additions} -{d.deletions})"
                )
        else:
            lines.append("[dim]Dependencies: no changes since last switch.[/dim]")
        lines.append("")

        if handoff.last_log_in_repo is not None:
            first_line = handoff.last_log_in_repo.message.splitlines()[0]
            rel = format_relative(handoff.last_log_in_repo.timestamp)
            lines.append(f"[bold]Your last log:[/bold] \"{first_line[:80]}\" ({rel})")

        if handoff.drift_error_count > 0:
            lines.append(f"[bold]Drift:[/bold] [red]{handoff.drift_error_count} error(s)[/red] — run `mship audit`")
        else:
            lines.append("[bold]Drift:[/bold] [green]clean[/green]")

        if handoff.test_status is None:
            lines.append("[bold]Tests:[/bold] not run yet")
        else:
            color = "green" if handoff.test_status == "pass" else "red"
            lines.append(f"[bold]Tests:[/bold] [{color}]{handoff.test_status}[/{color}]")

        for line in lines:
            output.print(line)
