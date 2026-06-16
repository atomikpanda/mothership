from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output


def _entry_to_dict(e) -> dict:
    """Stable JSON view of a LogEntry — all known kv fields (#101)."""
    return {
        "timestamp": e.timestamp.isoformat(),
        "message": e.message,
        "action": e.action,
        "repo": e.repo,
        "iteration": e.iteration,
        "test_state": e.test_state,
        "open_question": e.open_question,
        "id": e.id,
        "parent": e.parent,
        "evidence": e.evidence,
        "category": e.category,
    }


def _resolve_since_cutoff(value: str, entries: list):
    """Resolve a `--since` value to a cutoff datetime (#101).

    `last-phase-change` → timestamp of the most recent "Phase transition:"
    entry, or None if there is none (no filtering). Otherwise an ISO-8601
    timestamp (trailing `Z` accepted).
    """
    from datetime import datetime

    if value == "last-phase-change":
        for e in reversed(entries):
            if e.message.startswith("Phase transition:"):
                return e.timestamp
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def register(app: typer.Typer, get_container):
    @app.command(name="journal")
    def log_cmd(
        message: Optional[str] = typer.Argument(None, help="Message to append to task journal"),
        last: Optional[int] = typer.Option(None, "--last", help="Show only last N entries"),
        json_out: bool = typer.Option(False, "--json", help="Read: emit entries as a JSON array"),
        fmt: Optional[str] = typer.Option(None, "--format", help="Read: output format — json | jsonl"),
        since: Optional[str] = typer.Option(None, "--since", help="Read: only entries at/after an ISO timestamp or 'last-phase-change'"),
        action: Optional[str] = typer.Option(None, "--action", help="Structured: what you were doing"),
        open_question: Optional[str] = typer.Option(None, "--open", help="Structured: blocking question"),
        test_state: Optional[str] = typer.Option(None, "--test-state", help="Structured: pass|fail|mixed"),
        repo: Optional[str] = typer.Option(None, "--repo", help="Structured: which repo this entry is about"),
        iteration: Optional[int] = typer.Option(None, "--iteration", help="Structured: iteration number"),
        no_repo: bool = typer.Option(False, "--no-repo", help="Suppress active-repo inference"),
        show_open: bool = typer.Option(False, "--show-open", help="List open questions from this task's journal"),
        force: bool = typer.Option(False, "--force", "-f", help="Bypass cwd-outside-worktree check"),
        task_opt: Optional[str] = typer.Option(None, "--task", help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var."),
    ):
        """Append to or read the current task's journal."""
        from mship.util.duration import format_relative

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        resolved = resolve_for_command("log", state, task_opt, output)
        t = resolved.task

        log_mgr = container.log_manager()

        from pathlib import Path as _P
        from mship.cli._cwd_check import format_cwd_warning
        cwd_warn: str | None = None
        if t.active_repo is not None and t.active_repo in t.worktrees:
            cwd_warn = format_cwd_warning(_P.cwd(), _P(t.worktrees[t.active_repo]))

        # Export mode (#101): --json / --format turn the read path into a
        # machine-readable exporter. In that mode --action / --repo / --since
        # are read filters, not write-intent flags.
        export_mode = json_out or fmt is not None
        if fmt is not None and fmt not in ("json", "jsonl"):
            output.error("Invalid --format: use 'json' or 'jsonl'.")
            raise typer.Exit(code=1)

        # Structured-flag validation (#108): `mship journal --test-state pass`
        # (or --action / --open) without a message silently dropped the flag
        # and fell through to read mode. That made `--test-state pass`
        # ineffective as test-evidence — the unified reader (#81) had nothing
        # to read. Fail loud instead. (Skipped in export mode, where these
        # narrow the read instead of writing.)
        if message is None and not show_open and not export_mode and (
            test_state is not None or action is not None or open_question is not None
        ):
            output.error(
                "Structured journal flags require a message argument. Try:\n"
                "  mship journal \"tests verified externally\" --test-state pass"
            )
            raise typer.Exit(code=1)

        if show_open:
            entries = log_mgr.read(t.slug)
            opens = [e for e in entries if e.open_question]
            if not opens:
                if output.is_tty:
                    output.print("(no open questions)")
                else:
                    output.json({
                        "open_questions": [],
                        "resolved_task": resolved.task.slug,
                        "resolution_source": resolved.source,
                    })
                return
            if output.is_tty:
                output.print("[bold]Open questions:[/bold]")
                for e in opens:
                    rel = format_relative(e.timestamp)
                    repo_prefix = f"{e.repo}: " if e.repo else ""
                    output.print(f"  [{rel}] {repo_prefix}{e.open_question}")
            else:
                output.json({
                    "open_questions": [
                        {
                            "timestamp": e.timestamp.isoformat(),
                            "repo": e.repo,
                            "question": e.open_question,
                        }
                        for e in opens
                    ],
                    "resolved_task": resolved.task.slug,
                    "resolution_source": resolved.source,
                })
            return

        if message is not None:
            # cwd hard-error: writing path only
            if cwd_warn is not None:
                if not force:
                    output.error(cwd_warn)
                    output.error('Run from the worktree, or `mship journal --force "msg"` to override.')
                    raise typer.Exit(code=1)
                else:
                    # bypass: tag the entry so the bypass is discoverable
                    action = f"cwd-bypass,{action}" if action else "cwd-bypass"

            # Infer repo + iteration when not explicitly provided
            inferred_repo = repo
            if inferred_repo is None and not no_repo:
                inferred_repo = t.active_repo
            inferred_iter = iteration if iteration is not None else (
                t.test_iteration if t.test_iteration > 0 else None
            )
            log_mgr.append(
                t.slug, message,
                repo=inferred_repo,
                iteration=inferred_iter,
                test_state=test_state,
                action=action,
                open_question=open_question,
            )
            if output.is_tty:
                output.success("Logged")
            else:
                output.json({
                    "task": t.slug,
                    "logged": message,
                    "resolved_task": resolved.task.slug,
                    "resolution_source": resolved.source,
                })
            return

        # Read path (no message argument)
        entries = log_mgr.read(t.slug, last=last)

        # Read filters (#101): action / repo / since narrow the entries.
        if action is not None:
            entries = [e for e in entries if e.action == action]
        if repo is not None:
            entries = [e for e in entries if e.repo == repo]
        if since is not None:
            cutoff = _resolve_since_cutoff(since, log_mgr.read(t.slug))
            if cutoff is not None:
                entries = [e for e in entries if e.timestamp >= cutoff]

        # Export (#101): --json → a JSON array; --format jsonl → one object/line.
        if export_mode:
            import json as _json
            dicts = [_entry_to_dict(e) for e in entries]
            if fmt == "jsonl":
                for d in dicts:
                    typer.echo(_json.dumps(d))
            else:
                typer.echo(_json.dumps(dicts))
            return

        if not entries:
            output.print("No journal entries")
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
                "task": t.slug,
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
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })
