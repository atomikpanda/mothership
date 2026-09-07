from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Connection

from mship.core.persistence.database import WorkspaceDatabase
from mship.core.persistence.serialization import (
    ConcurrentUpdateError,
    PersistenceDecodeError,
)
from mship.core.persistence.task_repository import TaskRepository
from mship.core.persistence.schema import tasks
from mship.core.state import DependencyEdge, Task, TestResult

NOW = datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[Connection]:
    database = WorkspaceDatabase(tmp_path / ".mothership")
    database.initialize()
    with database.write() as conn:
        yield conn


def _full_task(slug: str = "task-a", *, with_dependencies: bool = False) -> Task:
    return Task(
        slug=slug,
        description="Persist every Task field",
        phase="review",
        created_at=NOW,
        affected_repos=["api", "web"],
        worktrees={
            "api": Path("/worktrees/api"),
            "tooling": Path("/worktrees/tooling"),
        },
        branch=f"feat/{slug}",
        test_results={
            "api": TestResult(status="pass", at=NOW + timedelta(minutes=1)),
            "web": TestResult(status="skip", at=NOW + timedelta(minutes=2)),
        },
        blocked_reason="waiting for review",
        blocked_at=NOW + timedelta(minutes=3),
        pr_urls={
            "api": "https://github.com/o/api/pull/1",
            "web": "https://github.com/o/web/pull/2",
        },
        finished_at=NOW + timedelta(minutes=4),
        phase_entered_at=NOW + timedelta(minutes=5),
        last_activity_at=NOW + timedelta(minutes=6),
        active_repo="api",
        last_switched_at_sha={
            "api": {"web": "abc123", "tooling": "def456"},
            "web": {"api": "987fed"},
        },
        test_iteration=4,
        base_branch="main",
        base_override="stack/base",
        passive_repos={"api", "docs"},
        spec_id="spec-a",
        depends_on=(
            [
                DependencyEdge(upstream_slug="up-a", created_at=NOW),
                DependencyEdge(
                    upstream_slug="up-b",
                    created_at=NOW + timedelta(seconds=1),
                ),
            ]
            if with_dependencies
            else []
        ),
        work_item_id="wi-a",
    )


def test_task_round_trips_every_model_field(connection: Connection) -> None:
    repository = TaskRepository()
    repository.insert(connection, _full_task("up-a"))
    repository.insert(connection, _full_task("up-b"))
    task = _full_task(with_dependencies=True)

    repository.insert(connection, task)
    restored = repository.get(connection, task.slug)

    assert restored is not None
    assert restored.model_dump(mode="json") == task.model_dump(mode="json")


def test_task_list_order_is_stable_by_slug(connection: Connection) -> None:
    repository = TaskRepository()
    repository.insert(connection, _full_task("task-b"))
    repository.insert(connection, _full_task("task-a"))

    assert list(repository.list(connection)) == ["task-a", "task-b"]


def test_replace_uses_optimistic_revision(connection: Connection) -> None:
    repository = TaskRepository()
    task = _full_task()
    repository.insert(connection, task)
    changed = task.model_copy(update={"description": "Changed"})

    assert repository.replace(connection, changed, expected_revision=0) == 1
    assert repository.get(connection, task.slug).description == "Changed"

    with pytest.raises(ConcurrentUpdateError, match="task-a"):
        repository.replace(connection, task, expected_revision=0)


def test_replace_changes_only_the_target_tasks_children(connection: Connection) -> None:
    repository = TaskRepository()
    task_a = _full_task("task-a")
    task_b = _full_task("task-b")
    repository.insert(connection, task_a)
    repository.insert(connection, task_b)

    changed_a = task_a.model_copy(
        update={
            "affected_repos": ["cli"],
            "worktrees": {"cli": Path("/worktrees/cli")},
            "test_results": {},
            "pr_urls": {},
            "depends_on": [],
        }
    )
    repository.replace(connection, changed_a)

    assert repository.get(connection, "task-a").model_dump(mode="json") == (
        changed_a.model_dump(mode="json")
    )
    assert repository.get(connection, "task-b").model_dump(mode="json") == (
        task_b.model_dump(mode="json")
    )


def test_malformed_task_row_names_table_and_entity(connection: Connection) -> None:
    repository = TaskRepository()
    repository.insert(connection, _full_task())
    connection.execute(
        tasks.update().where(tasks.c.slug == "task-a").values(created_at="not-a-datetime")
    )

    with pytest.raises(PersistenceDecodeError, match=r"tasks.*task-a"):
        repository.get(connection, "task-a")


def test_delete_reports_whether_task_existed(connection: Connection) -> None:
    repository = TaskRepository()
    repository.insert(connection, _full_task())

    assert repository.delete(connection, "task-a") is True
    assert repository.delete(connection, "task-a") is False
