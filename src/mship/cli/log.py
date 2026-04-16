from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(name="journal")
    def log_cmd(
        message: Optional[str] = typer.Argument(None, help="Message to append to task log"),
        last: Optional[int] = typer.Option(None, "--last", help="Show only last N entries"),
        action: Optional[str] = typer.Option(None, "--action", help="Structured: what you were doing"),
        open_question: Optional[str] = typer.Option(None, "--open", help="Structured: blocking question"),
        test_state: Optional[str] = typer.Option(None, "--test-state", help="Structured: pass|fail|mixed"),
        repo: Optional[str] = typer.Option(None, "--repo", help="Structured: which repo this entry is about"),
        iteration: Optional[int] = typer.Option(None, "--iteration", help="Structured: iteration number"),
        no_repo: bool = typer.Option(False, "--no-repo", help="Suppress active-repo inference"),
        show_open: bool = typer.Option(False, "--show-open", help="List open questions from this task's log"),
        force: bool = typer.Option(False, "--force", "-f", help="Bypass cwd-outside-worktree check"),
    ):
        """Append to or read the current task's log."""
        from mship.util.duration import format_relative

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn \"description\"` to start one.")
            raise typer.Exit(code=1)

        log_mgr = container.log_manager()
        task = state.tasks[state.current_task]

        from pathlib import Path as _P
        from mship.cli._cwd_check import format_cwd_warning
        cwd_warn: str | None = None
        if task.active_repo is not None and task.active_repo in task.worktrees:
            cwd_warn = format_cwd_warning(_P.cwd(), _P(task.worktrees[task.active_repo]))

        if show_open:
            entries = log_mgr.read(state.current_task)
            opens = [e for e in entries if e.open_question]
            if not opens:
                if output.is_tty:
                    output.print("(no open questions)")
                else:
                    output.json({"open_questions": []})
                return
            if output.is_tty:
                output.print("[bold]Open questions:[/bold]")
                for e in opens:
                    rel = format_relative(e.timestamp)
                    repo_prefix = f"{e.repo}: " if e.repo else ""
                    output.print(f"  [{rel}] {repo_prefix}{e.open_question}")
            else:
                output.json({"open_questions": [
                    {
                        "timestamp": e.timestamp.isoformat(),
                        "repo": e.repo,
                        "question": e.open_question,
                    }
                    for e in opens
                ]})
            return

        if message is not None:
            # cwd hard-error: writing path only
            if cwd_warn is not None:
                if not force:
                    output.error(cwd_warn)
                    output.error('Run from the worktree, or `mship log --force "msg"` to override.')
                    raise typer.Exit(code=1)
                else:
                    # bypass: tag the entry so the bypass is discoverable
                    action = f"cwd-bypass,{action}" if action else "cwd-bypass"

            # Infer repo + iteration when not explicitly provided
            inferred_repo = repo
            if inferred_repo is None and not no_repo:
                inferred_repo = task.active_repo
            inferred_iter = iteration if iteration is not None else (
                task.test_iteration if task.test_iteration > 0 else None
            )
            log_mgr.append(
                state.current_task, message,
                repo=inferred_repo,
                iteration=inferred_iter,
                test_state=test_state,
                action=action,
                open_question=open_question,
            )
            if output.is_tty:
                output.success("Logged")
            else:
                output.json({"task": state.current_task, "logged": message})
            return

        # Read path (no message argument)
        entries = log_mgr.read(state.current_task, last=last)
        if not entries:
            output.print("No log entries")
            return
        if output.is_tty:
            for entry in entries:
                output.print(f"[dim]{entry.timestamp.strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
                extras = []
                if entry.repo:
                    extras.append(f"repo={entry.repo}")
                if entry.iteration is not None:
                    extras.append(f"iter={entry.iteration}")
                if entry.test_state:
                    extras.append(f"test={entry.test_state}")
                if entry.action:
                    extras.append(f"action={entry.action}")
                if extras:
                    output.print(f"  [dim]{'  '.join(extras)}[/dim]")
                output.print(f"  {entry.message}")
                if entry.open_question:
                    output.print(f"  [yellow]open:[/yellow] {entry.open_question}")
        else:
            output.json({
                "task": state.current_task,
                "entries": [
                    {
                        "timestamp": e.timestamp.isoformat(),
                        "message": e.message,
                        "repo": e.repo,
                        "iteration": e.iteration,
                        "test_state": e.test_state,
                        "action": e.action,
                        "open_question": e.open_question,
                    }
                    for e in entries
                ],
            })
