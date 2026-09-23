from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from mship.cli.output import Output
from mship.core.persistence.database import DatabaseBusyError, DatabaseRevisionError
from mship.core.persistence.export import export_state, storage_status
from mship.core.persistence.migration import (
    MigrationPreflightError,
    MigrationVerificationError,
    OwnershipConflictError,
    migrate_state,
    preview_migration,
)


def _state_dir(get_container):
    container = get_container()
    return container.state_dir()


def _fail(out: Output, error: Exception) -> None:
    out.error(str(error))
    raise typer.Exit(1)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate owner resolution for task {key!r}")
        result[key] = value
    return result


def _load_owner_resolutions(path: Path) -> dict[str, str]:
    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"invalid owner resolution file {path}: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("owner resolution file must contain a JSON object")
    for task_slug, owner_id in parsed.items():
        if not task_slug:
            raise ValueError("owner resolution task slugs must be nonempty strings")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError(
                f"owner resolution for task {task_slug!r} must be a nonempty string"
            )
    return parsed


def _ownership_payload(plan: Any) -> dict[str, Any]:
    return asdict(plan)


def _render_ownership(out: Output, plan: Any) -> None:
    lines: list[str] = []
    for resolution in plan.resolutions:
        evidence = f" ({'; '.join(resolution.evidence)})" if resolution.evidence else ""
        removed = (
            f"; removed: {', '.join(resolution.removed_owner_ids)}"
            if resolution.removed_owner_ids
            else ""
        )
        lines.append(
            f"resolved {resolution.task_slug}: {resolution.owner_id} "
            f"via {resolution.rule}{removed}{evidence}"
        )
    for change in plan.changes:
        before = ", ".join(change.before) or "-"
        after = ", ".join(change.after) or "-"
        lines.append(f"work item {change.work_item_id}: {before} -> {after}")
    for change in plan.task_changes:
        lines.append(
            f"task {change.task_slug} work_item_id: {change.before!r} -> {change.after}"
        )
    for conflict in plan.conflicts:
        owners = ", ".join(conflict.owner_ids) or "-"
        evidence = f" ({'; '.join(conflict.evidence)})" if conflict.evidence else ""
        lines.append(
            f"conflict {conflict.task_slug}: {conflict.reason}; owners: {owners}{evidence}"
        )
    if lines:
        out.print("\n".join(lines))


def _fail_ownership(out: Output, plan: Any) -> None:
    if out.json_mode:
        out.json(_ownership_payload(plan))
    else:
        _render_ownership(out, plan)
        out.error("unresolved ownership conflicts")
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
    def migrate(
        preview: bool = typer.Option(
            False,
            "--preview",
            help="Show the migration and ownership plan without changing the workspace.",
        ),
        resolve_owners: Path | None = typer.Option(
            None,
            "--resolve-owners",
            metavar="FILE",
            help="JSON object mapping task slugs to existing WorkItem IDs.",
        ),
    ) -> None:
        """Explicitly migrate legacy storage or a known packaged SQLite revision."""
        out = Output()
        try:
            owner_resolutions = (
                _load_owner_resolutions(resolve_owners) if resolve_owners else None
            )
            state_dir = _state_dir(get_container)
            if preview:
                migration_preview = preview_migration(
                    state_dir,
                    owner_resolutions=owner_resolutions,
                )
            else:
                report = migrate_state(
                    state_dir,
                    owner_resolutions=owner_resolutions,
                )
        except OwnershipConflictError as error:
            _fail_ownership(out, error.plan)
        except (
            DatabaseBusyError,
            DatabaseRevisionError,
            MigrationPreflightError,
            MigrationVerificationError,
            OSError,
            ValidationError,
            ValueError,
        ) as error:
            _fail(out, error)

        if preview:
            payload = asdict(migration_preview)
            if out.json_mode:
                out.json(payload)
            else:
                out.print(
                    "\n".join(
                        (
                            f"backend: {migration_preview.backend}",
                            f"database: {migration_preview.database_path}",
                            f"revision: {migration_preview.revision}",
                            "migration required: "
                            f"{str(migration_preview.migration_required).lower()}",
                            "tasks: "
                            f"{migration_preview.tasks}; "
                            f"work items: {migration_preview.work_items}",
                        )
                    )
                )
                _render_ownership(out, migration_preview.ownership)
            if migration_preview.ownership.conflicts:
                raise typer.Exit(1)
            return

        payload = asdict(report)
        if out.json_mode:
            out.json(payload)
            return
        action = "migrated" if report.migrated else "already migrated"
        backup = str(report.backup_path) if report.backup_path else "none"
        summary = (
            f"{action}: {report.database_path}\n"
            f"revision: {report.revision}\n"
            f"tasks: {report.tasks}; work items: {report.work_items}\n"
            f"backup: {backup}"
        )
        if report.report_path:
            summary += f"\nreport: {report.report_path}"
        out.success(summary)
        if report.ownership is not None:
            _render_ownership(out, report.ownership)

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
