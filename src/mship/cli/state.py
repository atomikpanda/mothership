from __future__ import annotations

from dataclasses import asdict

import typer
from pydantic import ValidationError

from mship.cli.output import Output
from mship.core.persistence.database import DatabaseRevisionError
from mship.core.persistence.export import export_state, storage_status
from mship.core.persistence.migration import (
    MigrationPreflightError,
    MigrationVerificationError,
    migrate_legacy_state,
)


def _state_dir(get_container):
    container = get_container()
    return container.state_dir()


def _fail(out: Output, error: Exception) -> None:
    out.error(str(error))
    raise typer.Exit(1)


def register(parent: typer.Typer, get_container) -> None:
    state_app = typer.Typer(
        help="Inspect, migrate, and export workspace state storage.",
        no_args_is_help=True,
    )

    @state_app.command("status")
    def status() -> None:
        """Show the active backend and schema revision without changing state."""
        out = Output()
        try:
            result = storage_status(_state_dir(get_container))
        except (DatabaseRevisionError, OSError) as error:
            _fail(out, error)
        payload = result.as_dict()
        if out.json_mode:
            out.json(payload)
            return
        out.print(
            "\n".join(
                (
                    f"backend: {payload['backend']}",
                    f"database: {payload['database_path']}",
                    f"revision: {payload['current_revision'] or '-'}",
                    f"head revision: {payload['head_revision']}",
                    f"migration required: {str(payload['migration_required']).lower()}",
                )
            )
        )

    @state_app.command("migrate")
    def migrate() -> None:
        """Explicitly replace validated legacy files with transactional SQLite."""
        out = Output()
        try:
            report = migrate_legacy_state(_state_dir(get_container))
        except (
            DatabaseRevisionError,
            MigrationPreflightError,
            MigrationVerificationError,
            OSError,
            ValidationError,
            ValueError,
        ) as error:
            _fail(out, error)
        payload = asdict(report)
        if out.json_mode:
            out.json(payload)
            return
        action = "migrated" if report.migrated else "already migrated"
        backup = str(report.backup_path) if report.backup_path else "none"
        out.success(
            f"{action}: {report.database_path}\n"
            f"revision: {report.revision}\n"
            f"tasks: {report.tasks}; work items: {report.work_items}\n"
            f"backup: {backup}"
        )

    @state_app.command("export")
    def export(
        format: str = typer.Option(
            "json",
            "--format",
            help="Output format: json or yaml.",
        ),
    ) -> None:
        """Export deterministic Task and WorkItem records to stdout."""
        out = Output()
        try:
            rendered = export_state(
                _state_dir(get_container),
                format=format.lower(),
            )
        except (DatabaseRevisionError, OSError, ValidationError, ValueError) as error:
            _fail(out, error)
        typer.echo(rendered, nl=False)

    parent.add_typer(
        state_app,
        name="state",
        rich_help_panel="Work items & specs",
    )
