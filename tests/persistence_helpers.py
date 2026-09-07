from pathlib import Path

from mship.core.persistence.database import WorkspaceDatabase


def corrupt_workitem(state_dir: Path, item_id: str) -> None:
    """Fault-inject an unreadable WorkItem payload into an active SQLite store."""
    database = WorkspaceDatabase(state_dir)
    with database.write() as connection:
        connection.exec_driver_sql(
            "UPDATE work_items SET extras_json = ? WHERE id = ?",
            ("{", item_id),
        )
