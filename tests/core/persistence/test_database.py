from pathlib import Path

import pytest

from mship.core.persistence.database import (
    BUSY_TIMEOUT_MS,
    DB_FILENAME,
    DatabaseRevisionError,
    WorkspaceDatabase,
)


def test_database_path_is_state_dir_mothership_db(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"

    database = WorkspaceDatabase(state_dir)

    assert database.path == state_dir / DB_FILENAME
    assert database.path.name == "mothership.db"


def test_workspace_database_configures_required_sqlite_policy(tmp_path: Path) -> None:
    database = WorkspaceDatabase(tmp_path / ".mothership")
    database.initialize()

    with database.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == BUSY_TIMEOUT_MS
        assert connection.connection.driver_connection.autocommit is False


def test_explicit_write_rolls_back_on_exception(tmp_path: Path) -> None:
    database = WorkspaceDatabase(tmp_path / ".mothership")
    database.initialize()
    with database.write() as connection:
        connection.exec_driver_sql("CREATE TABLE rollback_probe (value INTEGER NOT NULL)")

    with pytest.raises(RuntimeError, match="abort transaction"):
        with database.write() as connection:
            connection.exec_driver_sql("INSERT INTO rollback_probe (value) VALUES (1)")
            raise RuntimeError("abort transaction")

    with database.read() as connection:
        count = connection.exec_driver_sql("SELECT COUNT(*) FROM rollback_probe").scalar_one()

    assert count == 0


def test_immediate_write_works_with_pep249_autocommit_disabled(
    tmp_path: Path,
) -> None:
    database = WorkspaceDatabase(tmp_path / ".mothership")
    database.initialize()

    with database.write(immediate=True) as connection:
        connection.exec_driver_sql(
            "CREATE TABLE immediate_probe (value INTEGER NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO immediate_probe (value) VALUES (1)"
        )

    with database.read() as connection:
        count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM immediate_probe"
        ).scalar_one()

    assert count == 1


def test_initialize_rejects_an_unknown_existing_revision(tmp_path: Path) -> None:
    database = WorkspaceDatabase(tmp_path / ".mothership")
    database.initialize()
    with database.write() as connection:
        connection.exec_driver_sql(
            "UPDATE alembic_version SET version_num = 'future_revision'"
        )

    with pytest.raises(DatabaseRevisionError, match="future_revision"):
        database.initialize()
