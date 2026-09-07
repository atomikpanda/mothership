from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Connection

from mship.core.persistence.database import WorkspaceDatabase
from mship.core.persistence.schema import work_items
from mship.core.persistence.serialization import (
    ConcurrentUpdateError,
    PersistenceDecodeError,
)
from mship.core.persistence.workitem_repository import WorkItemRepository
from mship.core.workitem import WorkItem

NOW = datetime(2026, 9, 7, 13, 0, tzinfo=timezone.utc)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[Connection]:
    database = WorkspaceDatabase(tmp_path / ".mothership")
    database.initialize()
    with database.write() as conn:
        yield conn


def _full_item(
    item_id: str = "wi-a",
    *,
    updated_at: datetime = NOW,
    archived: bool = False,
) -> WorkItem:
    return WorkItem.model_validate(
        {
            "id": item_id,
            "title": f"Item {item_id}",
            "workspace": "ws",
            "kind": "feature",
            "created_at": NOW,
            "updated_at": updated_at,
            "spec_id": "spec-a",
            "plan_path": "docs/plans/a.md",
            "task_slugs": ["task-b", "task-a"],
            "thread_ids": ["thread-b", "thread-a"],
            "external_links": [
                {
                    "provider": "github",
                    "url": "https://github.com/o/r/issues/1",
                    "title": "#1",
                },
                {
                    "provider": "url",
                    "url": "https://example.test/context",
                    "title": "Context",
                },
            ],
            "phase_override": "in_flight",
            "unattended": True,
            "archived": archived,
            "affected_repos": ["web", "api"],
            "pr_urls": [
                "https://github.com/o/web/pull/2",
                "https://github.com/o/api/pull/1",
            ],
            "future_field": {"preserve": True},
        }
    )


def test_workitem_round_trips_every_field_and_unknown_extras(
    connection: Connection,
) -> None:
    repository = WorkItemRepository()
    item = _full_item()

    repository.insert(connection, item)
    restored = repository.get(connection, item.id)

    assert restored is not None
    assert restored.model_dump(mode="json") == item.model_dump(mode="json")


def test_list_filters_archived_and_orders_newest_first(connection: Connection) -> None:
    repository = WorkItemRepository()
    old = _full_item("wi-old", updated_at=NOW)
    archived = _full_item(
        "wi-archived",
        updated_at=NOW + timedelta(minutes=2),
        archived=True,
    )
    new = _full_item("wi-new", updated_at=NOW + timedelta(minutes=1))
    for item in (old, archived, new):
        item.task_slugs = [f"{item.id}-task"]
        item.thread_ids = [f"{item.id}-thread"]
        repository.insert(connection, item)

    assert [item.id for item in repository.list(connection)] == ["wi-new", "wi-old"]
    assert [item.id for item in repository.list(connection, include_archived=True)] == [
        "wi-archived",
        "wi-new",
        "wi-old",
    ]


def test_replace_uses_optimistic_revision(connection: Connection) -> None:
    repository = WorkItemRepository()
    item = _full_item()
    repository.insert(connection, item)
    changed = item.model_copy(update={"title": "Changed"})

    assert repository.replace(connection, changed, expected_revision=0) == 1
    assert repository.get(connection, item.id).title == "Changed"

    with pytest.raises(ConcurrentUpdateError, match="wi-a"):
        repository.replace(connection, item, expected_revision=0)


def test_tolerant_list_skips_malformed_rows_and_reports_uncertainty(
    connection: Connection,
) -> None:
    repository = WorkItemRepository()
    good = _full_item("wi-good")
    good.task_slugs = ["good-task"]
    good.thread_ids = ["good-thread"]
    bad = _full_item("wi-bad")
    bad.task_slugs = ["bad-task"]
    bad.thread_ids = ["bad-thread"]
    repository.insert(connection, good)
    repository.insert(connection, bad)
    connection.execute(
        work_items.update()
        .where(work_items.c.id == "wi-bad")
        .values(extras_json="{malformed")
    )

    items, uncertain = repository.list_tolerant_with_uncertainty(connection)

    assert [item.id for item in items] == ["wi-good"]
    assert uncertain is True
    with pytest.raises(PersistenceDecodeError, match=r"work_items.*wi-bad"):
        repository.get(connection, "wi-bad")


def test_delete_reports_whether_workitem_existed(connection: Connection) -> None:
    repository = WorkItemRepository()
    repository.insert(connection, _full_item())

    assert repository.delete(connection, "wi-a") is True
    assert repository.delete(connection, "wi-a") is False
