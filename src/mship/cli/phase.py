import typer

from mship.cli.output import Output
from mship.core.phase import PHASE_ORDER


def register(app: typer.Typer, get_container):
    @app.command()
    def phase(target: str):
        """Transition the current task to a new phase."""
        container = get_container()
        output = Output()

        if target not in PHASE_ORDER:
            output.error(f"Invalid phase: {target}. Must be one of: {', '.join(PHASE_ORDER)}")
            raise typer.Exit(code=1)

        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task")
            raise typer.Exit(code=1)

        phase_mgr = container.phase_manager()
        result = phase_mgr.transition(state.current_task, target)

        for w in result.warnings:
            output.warning(w)

        if output.is_tty:
            output.success(f"Phase: {result.new_phase}")
        else:
            output.json({
                "task": state.current_task,
                "phase": result.new_phase,
                "warnings": result.warnings,
            })
