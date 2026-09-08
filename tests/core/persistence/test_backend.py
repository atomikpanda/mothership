from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from mship.core.persistence.backend import (
    StorageBackend,
    detect_backend,
    get_legacy_workitem,
)
from mship.core.persistence.database import WorkspaceDatabase
from mship.core.persistence.errors import LegacyMigrationRequired
from mship.core.persistence.task_repository import TaskRepository
from mship.core.persistence.workitem_repository import WorkItemRepository
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem import WorkItem
from mship.core.workitem_store import WorkItemStore

NOW = datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc)


def _task(slug: str) -> Task:
    return Task(
        slug=slug,
        description=slug,
        phase="plan",
        created_at=NOW,
        affected_repos=[],
        branch=f"feat/{slug}",
    )


def _item(item_id: str) -> WorkItem:
    return WorkItem(
        id=item_id,
        title=item_id,
        workspace="test",
        kind="feature",
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.parametrize(
    ("legacy_state", "legacy_items", "database", "expected"),
    [
        (False, False, False, StorageBackend.EMPTY),
        (True, False, False, StorageBackend.LEGACY),
        (False, True, False, StorageBackend.LEGACY),
        (True, True, True, StorageBackend.SQLITE),
    ],
)
def test_detect_backend(
    tmp_path: Path,
    legacy_state: bool,
    legacy_items: bool,
    database: bool,
    expected: StorageBackend,
) -> None:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    if legacy_state:
        (state_dir / "state.yaml").write_text("tasks: {}\n")
    if legacy_items:
        items_dir = state_dir / "workitems"
        items_dir.mkdir()
        (items_dir / "wi-a.json").write_text(_item("wi-a").model_dump_json())
    if database:
        WorkspaceDatabase(state_dir).initialize()

    assert detect_backend(state_dir) is expected


def test_legacy_state_is_readable_but_writes_require_migration(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state = WorkspaceState(tasks={"legacy": _task("legacy")})
    (state_dir / "state.yaml").write_text(
        yaml.safe_dump(state.model_dump(mode="json"))
    )
    manager = StateManager(state_dir)

    assert manager.load() == state
    with pytest.raises(LegacyMigrationRequired, match="mship state migrate"):
        manager.save(state)
    with pytest.raises(LegacyMigrationRequired, match="mship state migrate"):
        manager.mutate(lambda current: current.tasks.clear())


def test_legacy_workitems_are_readable_but_mutations_require_migration(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    items_dir = state_dir / "workitems"
    items_dir.mkdir(parents=True)
    item = _item("wi-legacy")
    (items_dir / "wi-legacy.json").write_text(item.model_dump_json())
    store = WorkItemStore(items_dir)

    assert store.get(item.id) == item
    with pytest.raises(LegacyMigrationRequired, match="mship state migrate"):
        store.link_spec(item.id, "spec-a", now=NOW)
    with pytest.raises(LegacyMigrationRequired, match="mship state migrate"):
        store.create("new", "feature", "test", NOW)


def test_legacy_backend_rejects_workitem_id_traversal(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    items_dir = state_dir / "workitems"
    items_dir.mkdir(parents=True)
    outside = _item("outside")
    (state_dir / "outside.json").write_text(outside.model_dump_json())

    with pytest.raises(ValueError, match="unsafe work item id"):
        get_legacy_workitem(items_dir, "../outside")


def test_legacy_workitem_list_rejects_symlink_outside_directory(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    items_dir = state_dir / "workitems"
    items_dir.mkdir(parents=True)
    outside = _item("outside")
    outside_path = state_dir / "outside.json"
    outside_path.write_text(outside.model_dump_json())
    (items_dir / "linked.json").symlink_to(outside_path)

    with pytest.raises(ValueError, match="unsafe work item id"):
        WorkItemStore(items_dir).list()


def test_empty_state_store_initializes_sqlite_on_first_write(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    manager = StateManager(state_dir)
    state = WorkspaceState(tasks={"task-a": _task("task-a")})

    manager.save(state)

    assert manager.load() == state
    assert (state_dir / "mothership.db").is_file()
    assert not (state_dir / "state.yaml").exists()


def test_empty_workitem_store_initializes_sqlite_on_first_write(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    store = WorkItemStore(state_dir / "workitems")

    item = store.create("new", "feature", "test", NOW)

    assert store.get(item.id) == item
    assert (state_dir / "mothership.db").is_file()
    assert not (state_dir / "workitems").exists()


def test_activated_database_wins_over_stale_legacy_files(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    database = WorkspaceDatabase(state_dir)
    database.initialize()
    with database.write() as connection:
        TaskRepository().insert(connection, _task("database-task"))
        WorkItemRepository().insert(connection, _item("wi-database"))

    (state_dir / "state.yaml").write_text(
        yaml.safe_dump(
            WorkspaceState(
                tasks={"stale-legacy-task": _task("stale-legacy-task")}
            ).model_dump(mode="json")
        )
    )
    items_dir = state_dir / "workitems"
    items_dir.mkdir()
    (items_dir / "wi-stale.json").write_text(_item("wi-stale").model_dump_json())

    assert list(StateManager(state_dir).load().tasks) == ["database-task"]
    assert [item.id for item in WorkItemStore(items_dir).list()] == ["wi-database"]
