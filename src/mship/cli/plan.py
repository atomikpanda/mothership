"""`mship plan` sub-app: implementation-plan self-checks.

Wave 1 (issue #444): the "Assumptions checked" coverage check is advisory
only — it always exits 0. The hard plan-gate block lands in Wave 3 once the
L1 assumption store exists; this command is the self-check a planner runs by
hand (or wires into their own pre-dev habit) in the meantime.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output


def register(parent: typer.Typer, get_container):
    plan_app = typer.Typer(
        name="plan",
        help="Implementation-plan checks.",
        no_args_is_help=True,
    )

    @plan_app.command("check-assumptions")
    def check_assumptions(
        task: Optional[str] = typer.Option(None, "--task", help="Task slug; resolves the plan via the docs/plans/<date>-<slug>.md convention."),
        plan: Optional[str] = typer.Option(None, "--plan", help="Explicit plan path (workspace-relative)."),
    ):
        """Report which seed assumption axes the plan's 'Assumptions checked'
        block leaves undispositioned. Advisory only — always exits 0."""
        from mship.core.assumptions import AssumptionStore, resolve_mode
        from mship.core.config import ConfigLoader
        from mship.core.plan import SEED_AXES, missing_assumption_axes, resolve_plan_path

        output = Output()
        container = get_container()

        if task is None and plan is None:
            output.error("Provide --task or --plan.")
            raise typer.Exit(1)

        config_path = Path(container.config_path())
        workspace_root = config_path.parent
        # Fall back to "docs" only when there is no config file (e.g. invoked
        # outside a materialized workspace); a present-but-malformed config must
        # surface, not be swallowed behind a plausible default.
        docs_dir = (
            ConfigLoader.load(config_path, require_paths=False).docs_dir
            if config_path.is_file()
            else "docs"
        )

        resolved = resolve_plan_path(task or "", plan, workspace_root, docs_dir)
        if resolved is None:
            where = f"for task {task!r}" if task else f"at {plan!r}"
            output.error(f"No plan found {where}.")
            raise typer.Exit(1)

        store = AssumptionStore(workspace_root, docs_dir=docs_dir, mode=resolve_mode(workspace_root))
        try:
            expected = store.axes() if store.path.is_file() else list(SEED_AXES)
        except ValueError as e:
            # A malformed store must fail loud, not silently shrink the expected
            # set and let an incomplete plan pass coverage (Greptile #450).
            output.error(str(e))
            raise typer.Exit(1)

        missing = missing_assumption_axes(resolved.read_text(), expected)
        ok = not missing

        if output.human_mode:
            output.print(f"Plan: {resolved}")
            if ok:
                output.success("All seed assumption axes dispositioned.")
            else:
                output.print("[yellow]Missing assumption axes:[/yellow]")
                for axis in missing:
                    output.print(f"  - {axis}")
        else:
            output.json({
                "plan": str(resolved),
                "expected": expected,
                "missing": missing,
                "ok": ok,
            })

    parent.add_typer(plan_app, rich_help_panel="Work items & specs")
