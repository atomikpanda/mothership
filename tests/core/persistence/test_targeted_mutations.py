from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import event

from mship.core.persistence.database import WorkspaceDatabase
from mship.core.persistence.workspace_store import WorkspaceStore
from mship.core.state import StateManager, Task, WorkspaceState


NOW = datetime(2026, 9, 7, 19, 0, tzinfo=timezone.utc)


def _task(slug: str) -> Task:
    return Task(
        slug=slug,
        description=f"Task {slug}",
        phase="dev",
        created_at=NOW,
        affected_repos=[f"repo-{slug}"],
        worktrees={f"repo-{slug}": Path(f"/tmp/{slug}")},
        branch=f"feat/{slug}",
    )


@pytest.fixture
def manager(tmp_path: Path) -> StateManager:
    return StateManager(tmp_path / ".mothership")


@pytest.fixture
def two_tasks(manager: StateManager) -> WorkspaceState:
    state = WorkspaceState(tasks={slug: _task(slug) for slug in ("a", "b")})
    manager.save(state)
    return state


def test_mutate_task_updates_only_named_task(
    manager: StateManager,
    two_tasks: WorkspaceState,
) -> None:
    before_other = manager.load().tasks["b"].model_dump(mode="json")

    result = manager.mutate_task(
        "a",
        lambda task: setattr(task, "blocked_reason", "wait"),
    )

    assert result.blocked_reason == "wait"
    assert manager.load().tasks["a"].blocked_reason == "wait"
    assert manager.load().tasks["b"].model_dump(mode="json") == before_other


def test_mutate_tasks_is_atomic_on_callback_failure(
    manager: StateManager,
    two_tasks: WorkspaceState,
) -> None:
    before = manager.load()

    def fail(tasks: dict[str, Task]) -> None:
        tasks["a"].blocked_reason = "changed"
        tasks["b"].blocked_reason = "changed"
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        manager.mutate_tasks(["a", "b"], fail)

    assert manager.load() == before


def test_mutate_tasks_updates_and_deletes_only_requested_rows(
    manager: StateManager,
) -> None:
    initial = WorkspaceState(tasks={slug: _task(slug) for slug in ("a", "b", "other")})
    manager.save(initial)
    before_other = manager.load().tasks["other"]

    def apply(tasks: dict[str, Task]) -> None:
        tasks["a"].blocked_reason = "wait"
        del tasks["b"]

    result = manager.mutate_tasks(["a", "b"], apply)
    state = manager.load()

    assert result == {"a": state.tasks["a"]}
    assert state.tasks["a"].blocked_reason == "wait"
    assert "b" not in state.tasks
    assert state.tasks["other"] == before_other


def test_mutate_task_does_not_rewrite_another_tasks_children(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    database = WorkspaceDatabase(state_dir)
    store = WorkspaceStore(state_dir, database=database)
    manager = StateManager(workspace_store=store)
    manager.save(WorkspaceState(tasks={slug: _task(slug) for slug in ("a", "b")}))
    writes: list[tuple[str, object]] = []

    @event.listens_for(database._engine, "before_cursor_execute")
    def capture_statement(
        _connection,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ) -> None:
        if statement.lstrip().upper().startswith(("DELETE", "INSERT", "UPDATE")):
            writes.append((statement, parameters))

    manager.mutate_task("a", lambda task: setattr(task, "active_repo", "repo-a"))

    assert writes
    assert all("b" not in repr(parameters) for _, parameters in writes)


def test_insert_and_delete_task_are_targeted(manager: StateManager) -> None:
    manager.insert_task(_task("a"))

    assert manager.load().tasks == {"a": _task("a")}
    assert manager.delete_task("missing") is False
    assert manager.delete_task("a") is True
    assert manager.load().tasks == {}


def test_targeted_mutations_can_join_a_caller_transaction(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    store = WorkspaceStore(state_dir)
    manager = StateManager(workspace_store=store)

    with pytest.raises(RuntimeError, match="rollback"):
        with store.write(immediate=True) as transaction:
            manager.insert_task(_task("a"), connection=transaction.connection)
            manager.mutate_task(
                "a",
                lambda task: setattr(task, "blocked_reason", "wait"),
                connection=transaction.connection,
            )
            raise RuntimeError("rollback")

    assert manager.load().tasks == {}
