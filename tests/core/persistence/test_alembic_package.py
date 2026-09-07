from importlib import resources
from pathlib import Path
import sqlite3

from alembic import command
from sqlalchemy import inspect

from mship.core.persistence.database import WorkspaceDatabase, make_alembic_config

EXPECTED_DOMAIN_TABLES = {
    "storage_metadata",
    "tasks",
    "task_repos",
    "task_test_results",
    "task_pr_urls",
    "task_switch_anchors",
    "task_dependencies",
    "work_items",
    "workitem_tasks",
    "workitem_threads",
    "workitem_external_links",
    "workitem_affected_repos",
    "workitem_pr_urls",
}


def test_initialize_upgrades_an_empty_database_to_alembic_head(tmp_path: Path) -> None:
    database = WorkspaceDatabase(tmp_path / ".mothership")

    database.initialize()

    assert database.current_revision() == database.head_revision()
    with database.connect() as connection:
        assert EXPECTED_DOMAIN_TABLES <= set(inspect(connection).get_table_names())


def test_alembic_downgrade_upgrade_round_trip(tmp_path: Path) -> None:
    database = WorkspaceDatabase(tmp_path / ".mothership")
    database.initialize()
    config = make_alembic_config(database.path)

    with database.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
    assert database.current_revision() is None

    with database.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    assert database.current_revision() == database.head_revision()


def test_initial_revision_is_a_package_resource() -> None:
    migration = resources.files("mship.core.persistence.alembic").joinpath(
        "versions",
        "0001_tasks_and_workitems.py",
    )

    assert migration.is_file()


def test_standalone_alembic_upgrade_enables_wal(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    database_path = state_dir / "mothership.db"

    command.upgrade(make_alembic_config(database_path), "head")

    with sqlite3.connect(database_path, autocommit=False) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
