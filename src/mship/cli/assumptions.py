"""`mship assumptions` sub-app: manage the L1 product-assumptions doc
(`docs/product_assumptions.md`) that `mship plan check-assumptions` checks
plans against (product-assumptions-wave-2)."""
from __future__ import annotations

from typing import Optional

import typer

from mship.cli.output import Output


def register(parent: typer.Typer, get_container):
    assumptions_app = typer.Typer(
        name="assumptions",
        help="Manage the L1 product-assumptions doc (`docs/product_assumptions.md`).",
        no_args_is_help=True,
    )

    def _store():
        """The workspace's assumption store in its configured
        `assumption_storage` mode. Mirrors `cli/spec.py`'s `_spec_store` —
        every verb reads/writes through the same mode-aware AssumptionStore."""
        from pathlib import Path
        from mship.core.assumptions import AssumptionStore, resolve_mode

        container = get_container()
        config_path = Path(container.config_path())
        workspace_root = config_path.parent
        mode = resolve_mode(workspace_root)
        return AssumptionStore(workspace_root, mode=mode)

    def _rows_to_dicts(rows) -> list[dict]:
        return [
            {
                "axis": r.axis,
                "options": r.options,
                "position": r.position,
                "triggers": r.triggers,
            }
            for r in rows
        ]

    @assumptions_app.command("list")
    def list_cmd():
        """List assumption rows, auto-seeding the store if absent."""
        output = Output()
        store = _store()
        rows = store.seed()

        if output.human_mode:
            output.table(
                "Assumptions",
                ["axis", "options", "position", "triggers"],
                [[r.axis, r.options, r.position, r.triggers] for r in rows],
            )
        else:
            output.json({
                "rows": _rows_to_dicts(rows),
                "count": len(rows),
                "path": str(store.path),
            })

    @assumptions_app.command("add")
    def add(
        axis: str = typer.Option(..., "--axis", help="Assumption axis name."),
        options: str = typer.Option(..., "--options", help="Candidate options for this axis."),
        position: str = typer.Option(..., "--position", help="The taken position."),
        triggers: str = typer.Option(..., "--triggers", help="Keywords/patterns that trigger this axis."),
    ):
        """Append a new assumption row."""
        from mship.core.assumptions import AssumptionRow

        output = Output()
        store = _store()
        rows = store.load()
        rows.append(AssumptionRow(axis=axis, options=options, position=position, triggers=triggers))
        warning = store.save(rows)
        if warning:
            output.warning(warning)

        if output.human_mode:
            output.success(f"Added assumption axis {axis!r}.")
        else:
            output.json({"axis": axis, "count": len(rows), "path": str(store.path)})

    @assumptions_app.command("edit")
    def edit(
        axis: str = typer.Argument(..., help="Axis name to edit (must exist)."),
        options: Optional[str] = typer.Option(None, "--options"),
        position: Optional[str] = typer.Option(None, "--position"),
        triggers: Optional[str] = typer.Option(None, "--triggers"),
    ):
        """Edit an existing assumption row's options/position/triggers."""
        from dataclasses import replace

        output = Output()
        store = _store()
        rows = store.load()

        idx = next((i for i, r in enumerate(rows) if r.axis == axis), None)
        if idx is None:
            output.error(f"No assumption axis {axis!r}. Known: {', '.join(r.axis for r in rows) or '(none)'}.")
            raise typer.Exit(1)

        updates = {}
        if options is not None:
            updates["options"] = options
        if position is not None:
            updates["position"] = position
        if triggers is not None:
            updates["triggers"] = triggers
        rows[idx] = replace(rows[idx], **updates)
        warning = store.save(rows)
        if warning:
            output.warning(warning)

        if output.human_mode:
            output.success(f"Updated assumption axis {axis!r}.")
        else:
            output.json({"axis": axis, "path": str(store.path)})

    @assumptions_app.command("render")
    def render():
        """Print the `## Assumptions checked` markdown block (for manual pasting
        into a plan, or L2 tooling)."""
        output = Output()
        store = _store()
        output.print(store.render())

    parent.add_typer(assumptions_app, rich_help_panel="Work items & specs")
