from typing import Optional

import typer

from mship.cli._resolve import resolve_or_exit
from mship.cli.output import Output
from mship.core.phase import PHASE_ORDER, FinishedTaskError


def register(app: typer.Typer, get_container):
    @app.command()
    def phase(
        target: str,
        force: bool = typer.Option(False, "--force", "-f", help="Force transition even if task is blocked or finished"),
        task: Optional[str] = typer.Option(None, "--task", help="Target task (default: cwd/env)"),
    ):
        """Transition a task to a new phase."""
        container = get_container()
        output = Output()

        if target not in PHASE_ORDER:
            output.error(f"Invalid phase: {target}. Must be one of: {', '.join(PHASE_ORDER)}")
            raise typer.Exit(code=1)

        state_mgr = container.state_manager()
        state = state_mgr.load()

        t = resolve_or_exit(state, task)

        if t.blocked_reason and not force:
            output.error(
                f"Task is blocked: {t.blocked_reason}. "
                f"Run `mship unblock` first, or `mship phase {target} --force` to unblock and transition."
            )
            raise typer.Exit(code=1)

        phase_mgr = container.phase_manager()
        try:
            result = phase_mgr.transition(
                t.slug,
                target,
                force_unblock=force,
                force_finished=force,
            )
        except FinishedTaskError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        for w in result.warnings:
            output.warning(w)

        if output.is_tty:
            output.success(f"Phase: {result.new_phase}")
        else:
            output.json({
                "task": t.slug,
                "phase": result.new_phase,
                "warnings": result.warnings,
            })
