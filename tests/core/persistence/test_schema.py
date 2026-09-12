from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError

from mship.core.persistence.database import WorkspaceDatabase
from mship.core.persistence.schema import (
    app_runs,
    metadata,
    task_dependencies,
    task_repos,
    tasks,
    work_items,
    workitem_external_links,
    workitem_tasks,
    workitem_threads,
)


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[Connection]:
    database = WorkspaceDatabase(tmp_path / ".mothership")
    database.initialize()
    with database.write() as conn:
        metadata.create_all(conn)
    with database.write() as conn:
        yield conn


def _seed_work_items(connection: Connection) -> None:
    connection.execute(
        work_items.insert(),
        [
            {
                "id": "wi-a",
                "title": "A",
                "workspace": "test",
                "kind": "feature",
                "created_at": "2026-09-07T00:00:00+00:00",
                "updated_at": "2026-09-07T00:00:00+00:00",
            },
            {
                "id": "wi-b",
                "title": "B",
                "workspace": "test",
                "kind": "bug",
                "created_at": "2026-09-07T00:00:00+00:00",
                "updated_at": "2026-09-07T00:00:00+00:00",
            },
        ],
    )


def _task_row(slug: str = "task-a", **updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "slug": slug,
        "description": "Task",
        "phase": "dev",
        "created_at": "2026-09-07T00:00:00+00:00",
        "branch": f"feat/{slug}",
    }
    row.update(updates)
    return row


def _app_run_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "run-a",
        "task_slug": "task-a",
        "repo": "api",
        "profile": "ios-development",
        "profile_revision": "a" * 64,
        "backend": "flutter",
        "backend_revision": "adapter-r2",
        "host_name": "studio",
        "host_scope": "project",
        "host_endpoint_fingerprint": "b" * 64,
        "safe_target_label": "iPhone 16",
        "private_binding_ref": "c" * 43,
        "operation": "run",
        "protocol_version": 1,
        "capabilities_json": "[\"run\"]",
        "owner_ref": None,
        "owner_generation": None,
        "status": "starting",
        "revision": 0,
        "created_at": "2026-09-12T22:30:00+00:00",
        "updated_at": "2026-09-12T22:30:00+00:00",
        "binary_provenance_json": None,
    }
    row.update(updates)
    return row


def test_one_task_slug_has_one_workitem_owner(connection: Connection) -> None:
    _seed_work_items(connection)
    connection.execute(
        workitem_tasks.insert().values(work_item_id="wi-a", task_slug="task-a", ordinal=0)
    )

    with pytest.raises(IntegrityError):
        connection.execute(
            workitem_tasks.insert().values(
                work_item_id="wi-b",
                task_slug="task-a",
                ordinal=0,
            )
        )


def test_thread_has_one_workitem_owner(connection: Connection) -> None:
    _seed_work_items(connection)
    connection.execute(
        workitem_threads.insert().values(
            work_item_id="wi-a",
            thread_id="thread-a",
            ordinal=0,
        )
    )

    with pytest.raises(IntegrityError):
        connection.execute(
            workitem_threads.insert().values(
                work_item_id="wi-b",
                thread_id="thread-a",
                ordinal=0,
            )
        )


def test_workitem_children_require_an_existing_parent(connection: Connection) -> None:
    with pytest.raises(IntegrityError):
        connection.execute(
            workitem_tasks.insert().values(
                work_item_id="missing",
                task_slug="task-a",
                ordinal=0,
            )
        )


@pytest.mark.parametrize("phase", ["inbox", "finished", ""])
def test_task_phase_is_constrained(connection: Connection, phase: str) -> None:
    with pytest.raises(IntegrityError):
        connection.execute(tasks.insert().values(**_task_row(phase=phase)))


@pytest.mark.parametrize(
    ("column", "value"),
    [("revision", -1), ("test_iteration", -1)],
)
def test_task_counters_are_non_negative(
    connection: Connection,
    column: str,
    value: int,
) -> None:
    with pytest.raises(IntegrityError):
        connection.execute(tasks.insert().values(**_task_row(**{column: value})))


def test_child_ordinals_are_unique_per_parent(connection: Connection) -> None:
    _seed_work_items(connection)
    connection.execute(
        workitem_external_links.insert().values(
            work_item_id="wi-a",
            ordinal=0,
            provider="github",
            url="https://example.test/one",
            title="One",
        )
    )

    with pytest.raises(IntegrityError):
        connection.execute(
            workitem_external_links.insert().values(
                work_item_id="wi-a",
                ordinal=0,
                provider="url",
                url="https://example.test/two",
                title="Two",
            )
        )


def test_affected_repo_ordinals_are_unique_per_task(connection: Connection) -> None:
    connection.execute(tasks.insert().values(**_task_row()))
    connection.execute(
        task_repos.insert().values(
            task_slug="task-a",
            repo_name="repo-a",
            affected_ordinal=0,
            passive=False,
        )
    )

    with pytest.raises(IntegrityError):
        connection.execute(
            task_repos.insert().values(
                task_slug="task-a",
                repo_name="repo-b",
                affected_ordinal=0,
                passive=False,
            )
        )


def test_task_cannot_depend_on_itself(connection: Connection) -> None:
    connection.execute(tasks.insert().values(**_task_row()))

    with pytest.raises(IntegrityError):
        connection.execute(
            task_dependencies.insert().values(
                task_slug="task-a",
                upstream_slug="task-a",
                created_at="2026-09-07T00:00:00+00:00",
            )
        )


def test_app_run_requires_exact_task_repo_and_blocks_task_cascade(
    connection: Connection,
) -> None:
    connection.execute(tasks.insert().values(**_task_row()))

    with pytest.raises(IntegrityError):
        connection.execute(app_runs.insert().values(**_app_run_row()))

    connection.execute(
        task_repos.insert().values(
            task_slug="task-a",
            repo_name="api",
            affected_ordinal=0,
            passive=False,
        )
    )
    with pytest.raises(IntegrityError):
        connection.execute(app_runs.insert().values(**_app_run_row(status="active")))

    with pytest.raises(IntegrityError):
        connection.execute(app_runs.insert().values(**_app_run_row(status="running")))

    connection.execute(app_runs.insert().values(**_app_run_row()))
    with pytest.raises(IntegrityError):
        connection.execute(tasks.delete().where(tasks.c.slug == "task-a"))
