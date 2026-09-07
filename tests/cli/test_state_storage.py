import json
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.persistence.database import WorkspaceDatabase


runner = CliRunner()


@pytest.fixture
def state_cli(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)
    try:
        yield state_dir
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.workspace_store.reset()
        container.workspace_database.reset()


def test_state_status_reports_stable_legacy_payload(state_cli: Path) -> None:
    (state_cli / "state.yaml").write_text("tasks: {}\n")

    result = runner.invoke(app, ["--json", "state", "status"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "backend": "legacy",
        "database_path": str(state_cli / "mothership.db"),
        "current_revision": None,
        "head_revision": "0001_tasks_and_workitems",
        "migration_required": True,
    }
    assert not (state_cli / "mothership.db").exists()


def test_state_export_emits_machine_readable_json(state_cli: Path) -> None:
    (state_cli / "state.yaml").write_text("tasks: {}\n")

    result = runner.invoke(app, ["state", "export", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"tasks": [], "work_items": []}


def test_state_export_rejects_unsupported_format(state_cli: Path) -> None:
    result = runner.invoke(app, ["state", "export", "--format", "toml"])

    assert result.exit_code == 1
    assert "json or yaml" in result.output


def test_state_migrate_refuses_running_daemon(
    state_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (state_cli / "state.yaml").write_text("tasks: {}\n")
    monkeypatch.setattr(
        "mship.core.persistence.migration.daemon_is_running",
        lambda: True,
    )

    result = runner.invoke(app, ["state", "migrate"])

    assert result.exit_code == 1
    assert "daemon is running" in result.output


def test_state_migrate_rejects_invalid_legacy_data(
    state_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (state_cli / "state.yaml").write_text("tasks: [not-a-mapping]\n")
    monkeypatch.setattr(
        "mship.core.persistence.migration.daemon_is_running",
        lambda: False,
    )

    result = runner.invoke(app, ["state", "migrate"])

    assert result.exit_code == 1
    assert "validation" in result.output.lower()
    assert not (state_cli / "mothership.db").exists()


def test_state_migrate_is_idempotent(
    state_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (state_cli / "state.yaml").write_text("tasks: {}\n")
    monkeypatch.setattr(
        "mship.core.persistence.migration.daemon_is_running",
        lambda: False,
    )

    first = runner.invoke(app, ["--json", "state", "migrate"])
    second = runner.invoke(app, ["--json", "state", "migrate"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert json.loads(first.output)["migrated"] is True
    assert json.loads(second.output)["migrated"] is False


def test_state_status_and_export_use_sqlite_after_migration(
    state_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (state_cli / "state.yaml").write_text("tasks: {}\n")
    monkeypatch.setattr(
        "mship.core.persistence.migration.daemon_is_running",
        lambda: False,
    )

    migrated = runner.invoke(app, ["--json", "state", "migrate"])
    status = runner.invoke(app, ["--json", "state", "status"])
    exported = runner.invoke(app, ["state", "export", "--format", "json"])

    assert migrated.exit_code == 0, migrated.output
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload == {
        "backend": "sqlite",
        "database_path": str(state_cli / "mothership.db"),
        "current_revision": "0001_tasks_and_workitems",
        "head_revision": "0001_tasks_and_workitems",
        "migration_required": False,
    }
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.output) == {"tasks": [], "work_items": []}


@pytest.mark.parametrize("revision", ["behind_revision", "future_revision"])
def test_state_commands_explain_incompatible_revision(
    state_cli: Path,
    revision: str,
) -> None:
    database = WorkspaceDatabase(state_cli)
    database.initialize()
    with database.write() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": revision},
        )

    result = runner.invoke(app, ["state", "migrate"])

    assert result.exit_code == 1
    assert revision in result.output
    assert "requires '0001_tasks_and_workitems'" in result.output
