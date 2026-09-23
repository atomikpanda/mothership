import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from alembic import command
from sqlalchemy import text
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.persistence.database import (
    DatabaseBusyError,
    WorkspaceDatabase,
    make_alembic_config,
)
from mship.core.state import Task, WorkspaceState
from mship.core.workitem import WorkItem


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


def _legacy_files(state_dir: Path) -> dict[str, bytes]:
    return {
        path.relative_to(state_dir).as_posix(): path.read_bytes()
        for path in sorted(state_dir.rglob("*"))
        if path.is_file()
    }


def _seed_ambiguous_owners(state_dir: Path) -> None:
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    tasks = {
        slug: Task(
            slug=slug,
            description=f"legacy {slug}",
            phase="dev",
            created_at=now,
            affected_repos=[],
            branch=f"feat/{slug}",
        )
        for slug in ("task-a", "task-b")
    }
    (state_dir / "state.yaml").write_text(
        yaml.safe_dump(WorkspaceState(tasks=tasks).model_dump(mode="json"))
    )
    workitems_dir = state_dir / "workitems"
    workitems_dir.mkdir()
    for item_id in ("wi-first", "wi-second"):
        item = WorkItem(
            id=item_id,
            title=item_id,
            workspace="test",
            kind="chore",
            created_at=now,
            updated_at=now,
            task_slugs=["task-a", "task-b"],
        )
        (workitems_dir / f"{item_id}.json").write_text(item.model_dump_json())


def test_state_status_reports_stable_legacy_payload(state_cli: Path) -> None:
    (state_cli / "state.yaml").write_text("tasks: {}\n")
    result = runner.invoke(app, ["--json", "state", "status"])

    head = WorkspaceDatabase(state_cli).head_revision()
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "backend": "legacy",
        "database_path": str(state_cli / "mothership.db"),
        "current_revision": None,
        "head_revision": head,
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


def test_state_migrate_reports_busy_database(
    state_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mship.cli.state.migrate_state",
        lambda _state_dir, **_kwargs: (_ for _ in ()).throw(
            DatabaseBusyError("database busy")
        ),
    )

    result = runner.invoke(app, ["state", "migrate"])

    assert result.exit_code == 1
    assert "database busy" in result.output


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


def test_state_migrate_upgrades_known_sqlite_ancestor(state_cli: Path) -> None:
    database = WorkspaceDatabase(state_cli)
    database.initialize()
    with database.connect() as connection:
        config = make_alembic_config(database.path)
        config.attributes["connection"] = connection
        command.downgrade(config, "0001_tasks_and_workitems")

    result = runner.invoke(app, ["--json", "state", "migrate"])
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["migrated"] is True
    assert Path(payload["backup_path"]).is_file()
    assert database.current_revision() == database.head_revision()


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

    head = WorkspaceDatabase(state_cli).head_revision()
    assert migrated.exit_code == 0, migrated.output
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload == {
        "backend": "sqlite",
        "database_path": str(state_cli / "mothership.db"),
        "current_revision": head,
        "head_revision": head,
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
    assert "requires" in result.output


def test_state_migrate_preview_aggregates_conflicts_without_mutation(
    state_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_ambiguous_owners(state_cli)
    before = _legacy_files(state_cli)
    monkeypatch.setattr(
        "mship.core.persistence.migration.daemon_is_running",
        lambda: False,
    )

    result = runner.invoke(app, ["--json", "state", "migrate", "--preview"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert {
        conflict["task_slug"] for conflict in payload["ownership"]["conflicts"]
    } == {"task-a", "task-b"}
    assert _legacy_files(state_cli) == before
    assert not (state_cli / "mothership.db").exists()
    blocked = runner.invoke(app, ["--json", "state", "migrate"])
    assert blocked.exit_code == 1, blocked.output
    assert {
        conflict["task_slug"] for conflict in json.loads(blocked.output)["conflicts"]
    } == {"task-a", "task-b"}
    assert not (state_cli / "mothership.db").exists()


def test_state_migrate_retries_ownership_preview_with_operator_map(
    state_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_ambiguous_owners(state_cli)
    initial = runner.invoke(app, ["--json", "state", "migrate", "--preview"])
    owner_map = state_cli / "owners.json"
    owner_map.write_text(json.dumps({"task-a": "wi-first", "task-b": "wi-second"}))
    monkeypatch.setattr(
        "mship.core.persistence.migration.daemon_is_running",
        lambda: False,
    )

    result = runner.invoke(
        app,
        [
            "--json",
            "state",
            "migrate",
            "--resolve-owners",
            str(owner_map),
        ],
    )

    assert initial.exit_code == 1, initial.output
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ownership"]["conflicts"] == []
    assert {
        resolution["task_slug"]: resolution["owner_id"]
        for resolution in payload["ownership"]["resolutions"]
    } == {"task-a": "wi-first", "task-b": "wi-second"}
    assert Path(payload["report_path"]).is_file()
    assert (state_cli / "mothership.db").is_file()
    exported = runner.invoke(app, ["state", "export", "--format", "json"])
    assert exported.exit_code == 0, exported.output
    task_slugs = {
        item["id"]: item["task_slugs"]
        for item in json.loads(exported.output)["work_items"]
    }
    assert task_slugs == {
        "wi-first": ["task-a"],
        "wi-second": ["task-b"],
    }
    repeated = runner.invoke(
        app,
        ["--json", "state", "migrate", "--resolve-owners", str(owner_map)],
    )
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(repeated.output)["migrated"] is False
    assert json.loads(repeated.output)["backup_path"] is None
    owner_map.write_text(json.dumps({"task-a": "wi-second"}))
    changed = runner.invoke(
        app,
        ["--json", "state", "migrate", "--resolve-owners", str(owner_map)],
    )
    assert changed.exit_code == 1, changed.output
    assert [c["task_slug"] for c in json.loads(changed.output)["conflicts"]] == [
        "task-a"
    ]
    after = runner.invoke(app, ["state", "export", "--format", "json"])
    assert json.loads(after.output) == json.loads(exported.output)


@pytest.mark.parametrize(
    "contents",
    [
        "[]",
        '{"task-a": ""}',
        '{"task-a": "wi-first", "task-a": "wi-second"}',
    ],
)
def test_state_migrate_rejects_invalid_owner_resolution_map(
    state_cli: Path,
    contents: str,
) -> None:
    _seed_ambiguous_owners(state_cli)
    owner_map = state_cli / "owners.json"
    owner_map.write_text(contents)
    before = _legacy_files(state_cli)

    result = runner.invoke(
        app,
        ["state", "migrate", "--resolve-owners", str(owner_map)],
    )

    assert result.exit_code == 1
    assert _legacy_files(state_cli) == before
    assert not (state_cli / "mothership.db").exists()
