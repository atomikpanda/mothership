"""`mship debug` sub-app — structured journal entries for debugging. See #30.

Three commands: `hypothesis`, `rule-out`, `resolved`. Each writes a single
journal entry via `LogManager.append`. Auto-generates an 8-char hex id when
the user doesn't provide `--id`. Advisory stderr warning on `resolved` without
any prior hypothesis in the journal.
"""
import uuid
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output


def _auto_id() -> str:
    """Short UUID prefix (8 hex chars). Collision odds are fine for per-task volumes."""
    return uuid.uuid4().hex[:8]


def register(app: typer.Typer, get_container):
    debug_app = typer.Typer(help="Structured journal entries for debugging. See #30.")

    @debug_app.command()
    def hypothesis(
        text: str = typer.Argument(..., help="Hypothesis statement"),
        evidence: Optional[str] = typer.Option(
            None, "--evidence",
            help="Free-form evidence ref (e.g. test-runs/5, HEAD, path:12-18)",
        ),
        id_: Optional[str] = typer.Option(
            None, "--id",
            help="Human-readable handle (default: auto 8-char hex)",
        ),
        task: Optional[str] = typer.Option(
            None, "--task",
            help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env.",
        ),
    ):
        """Log a debugging hypothesis."""
        container = get_container()
        output = Output()
        state = container.state_manager().load()
        resolved = resolve_for_command("debug", state, task, output)
        entry_id = id_ if id_ else _auto_id()
        container.log_manager().append(
            resolved.task.slug, text,
            action="hypothesis",
            id=entry_id,
            evidence=evidence,
        )

    @debug_app.command(name="rule-out")
    def rule_out(
        text: str = typer.Argument(..., help="Why the hypothesis is ruled out"),
        parent: Optional[str] = typer.Option(
            None, "--parent", help="id of the hypothesis being refuted",
        ),
        evidence: Optional[str] = typer.Option(
            None, "--evidence", help="Evidence ref refuting the hypothesis",
        ),
        category: Optional[str] = typer.Option(
            None, "--category",
            help="Optional classification (e.g. 'tool-output-misread')",
        ),
        id_: Optional[str] = typer.Option(
            None, "--id", help="Handle for this rule-out entry",
        ),
        task: Optional[str] = typer.Option(None, "--task"),
    ):
        """Log a ruled-out hypothesis."""
        container = get_container()
        output = Output()
        state = container.state_manager().load()
        resolved = resolve_for_command("debug", state, task, output)
        entry_id = id_ if id_ else _auto_id()
        container.log_manager().append(
            resolved.task.slug, text,
            action="ruled-out",
            id=entry_id,
            parent=parent,
            evidence=evidence,
            category=category,
        )

    @debug_app.command()
    def resolved(
        text: str = typer.Argument(..., help="Root cause + fix summary"),
        id_: Optional[str] = typer.Option(None, "--id"),
        task: Optional[str] = typer.Option(None, "--task"),
    ):
        """Close the open debug thread."""
        container = get_container()
        output = Output()
        state = container.state_manager().load()
        resolved_task = resolve_for_command("debug", state, task, output)

        # Advisory: warn if no prior hypothesis entry exists since the most
        # recent debug-resolved (or ever). Journal write succeeds regardless.
        log = container.log_manager()
        entries = log.read(resolved_task.task.slug)
        last_resolved_idx = -1
        for i, e in enumerate(entries):
            if e.action == "debug-resolved":
                last_resolved_idx = i
        has_hypothesis_in_segment = any(
            e.action == "hypothesis"
            for e in entries[last_resolved_idx + 1 :]
        )
        if not has_hypothesis_in_segment:
            output.warning(
                "logging debug-resolved without any prior hypothesis entries in the current segment"
            )

        entry_id = id_ if id_ else _auto_id()
        log.append(
            resolved_task.task.slug, text,
            action="debug-resolved",
            id=entry_id,
        )

    app.add_typer(debug_app, name="debug")
