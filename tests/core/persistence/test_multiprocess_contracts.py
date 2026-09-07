from __future__ import annotations

import json
import multiprocessing
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.persistence import database as database_module
from mship.core.persistence.database import DatabaseBusyError, WorkspaceDatabase
from mship.core.persistence.lifecycle_repository import LifecycleRepository
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem_store import TaskLinkAmbiguousError, WorkItemStore


NOW = datetime(2026, 9, 7, 22, 0, tzinfo=timezone.utc)
ROUNDS = 20
MUTATIONS_PER_WORKER = 50
PROCESS_TIMEOUT_SECONDS = 10


def _task(slug: str) -> Task:
    return Task(
        slug=slug,
        description=slug,
        phase="dev",
        created_at=NOW,
        affected_repos=[],
        branch=f"feat/{slug}",
    )


def _round_mutations(round_index: int) -> int:
    return 3 if round_index < 10 else 2


def _mutate_task_worker(state_dir: str, slug: str, barrier) -> None:
    manager = StateManager(Path(state_dir))
    for round_index in range(ROUNDS):
        barrier.wait(timeout=PROCESS_TIMEOUT_SECONDS)
        for _ in range(_round_mutations(round_index)):
            manager.mutate_task(
                slug,
                lambda task: setattr(
                    task,
                    "test_iteration",
                    task.test_iteration + 1,
                ),
            )


def _mutate_whole_snapshot_worker(state_dir: str, slug: str, barrier) -> None:
    manager = StateManager(Path(state_dir))
    for round_index in range(ROUNDS):
        barrier.wait(timeout=PROCESS_TIMEOUT_SECONDS)
        for _ in range(_round_mutations(round_index)):
            manager.mutate(
                lambda state: setattr(
                    state.tasks[slug],
                    "test_iteration",
                    state.tasks[slug].test_iteration + 1,
                ),
            )


def _link_worker(
    state_dir: str,
    task_item_pairs: list[tuple[str, str]],
    barrier,
) -> None:
    lifecycle = LifecycleRepository(StateManager(Path(state_dir)).workspace_store)
    for task_slug, item_id in task_item_pairs:
        barrier.wait(timeout=PROCESS_TIMEOUT_SECONDS)
        lifecycle.link_task(item_id, task_slug, now=NOW)


def _duplicate_link_worker(
    state_dir: str,
    item_id: str,
    result_path: str,
    barrier,
) -> None:
    lifecycle = LifecycleRepository(StateManager(Path(state_dir)).workspace_store)
    barrier.wait(timeout=PROCESS_TIMEOUT_SECONDS)
    try:
        lifecycle.link_task(item_id, "shared-task", now=NOW)
    except TaskLinkAmbiguousError:
        outcome = "duplicate"
    else:
        outcome = "linked"
    Path(result_path).write_text(outcome)


def _registration_worker(
    state_dir: str,
    item_ids: list[str],
    result_path: str,
    barrier,
) -> None:
    lifecycle = LifecycleRepository(StateManager(Path(state_dir)).workspace_store)
    outcomes: list[str] = []
    for round_index, item_id in enumerate(item_ids):
        barrier.wait(timeout=PROCESS_TIMEOUT_SECONDS)
        task = _task(f"registration-{round_index}")
        try:
            lifecycle.register_task(task, item_id, now=NOW)
        except KeyError, TaskLinkAmbiguousError:
            outcomes.append("duplicate")
        else:
            outcomes.append("registered")
    Path(result_path).write_text(json.dumps(outcomes))


def _run_pair(ctx, target, args_a: tuple, args_b: tuple) -> None:
    barrier = ctx.Barrier(2)
    processes = [
        ctx.Process(target=target, args=(*args_a, barrier)),
        ctx.Process(target=target, args=(*args_b, barrier)),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=PROCESS_TIMEOUT_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join(timeout=PROCESS_TIMEOUT_SECONDS)
            pytest.fail(f"multiprocess worker exceeded {PROCESS_TIMEOUT_SECONDS}s")
        assert process.exitcode == 0, f"worker exited with {process.exitcode}"


def test_concurrent_different_task_mutations_never_lose_updates(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    manager = StateManager(state_dir)
    manager.save(
        WorkspaceState(tasks={"task-a": _task("task-a"), "task-b": _task("task-b")})
    )
    ctx = multiprocessing.get_context("spawn")

    _run_pair(
        ctx,
        _mutate_task_worker,
        (str(state_dir), "task-a"),
        (str(state_dir), "task-b"),
    )

    state = manager.load()
    assert state.tasks["task-a"].test_iteration == MUTATIONS_PER_WORKER
    assert state.tasks["task-b"].test_iteration == MUTATIONS_PER_WORKER


def test_concurrent_same_task_mutations_serialize_without_loss(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    manager = StateManager(state_dir)
    manager.save(WorkspaceState(tasks={"shared-task": _task("shared-task")}))
    ctx = multiprocessing.get_context("spawn")

    _run_pair(
        ctx,
        _mutate_task_worker,
        (str(state_dir), "shared-task"),
        (str(state_dir), "shared-task"),
    )

    assert manager.load().tasks["shared-task"].test_iteration == (
        MUTATIONS_PER_WORKER * 2
    )


def test_concurrent_whole_snapshot_mutations_remain_compatible(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    manager = StateManager(state_dir)
    manager.save(
        WorkspaceState(tasks={"task-a": _task("task-a"), "task-b": _task("task-b")})
    )
    ctx = multiprocessing.get_context("spawn")

    _run_pair(
        ctx,
        _mutate_whole_snapshot_worker,
        (str(state_dir), "task-a"),
        (str(state_dir), "task-b"),
    )

    state = manager.load()
    assert state.tasks["task-a"].test_iteration == MUTATIONS_PER_WORKER
    assert state.tasks["task-b"].test_iteration == MUTATIONS_PER_WORKER


def test_concurrent_workitem_links_commit_both_sides(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    manager = StateManager(state_dir)
    tasks = {
        f"task-{worker}-{round_index}": _task(f"task-{worker}-{round_index}")
        for worker in ("a", "b")
        for round_index in range(ROUNDS)
    }
    manager.save(WorkspaceState(tasks=tasks))
    items = WorkItemStore(state_dir / "workitems")
    pairs: dict[str, list[tuple[str, str]]] = {"a": [], "b": []}
    for worker in ("a", "b"):
        for round_index in range(ROUNDS):
            task_slug = f"task-{worker}-{round_index}"
            item = items.create(task_slug, "chore", "test", NOW)
            pairs[worker].append((task_slug, item.id))
    ctx = multiprocessing.get_context("spawn")

    _run_pair(
        ctx,
        _link_worker,
        (str(state_dir), pairs["a"]),
        (str(state_dir), pairs["b"]),
    )

    state = manager.load()
    for task_slug, item_id in [*pairs["a"], *pairs["b"]]:
        assert state.tasks[task_slug].work_item_id == item_id
        assert items.get(item_id).task_slugs == [task_slug]


def test_concurrent_duplicate_task_ownership_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    manager = StateManager(state_dir)
    manager.save(WorkspaceState(tasks={"shared-task": _task("shared-task")}))
    items = WorkItemStore(state_dir / "workitems")
    first = items.create("First", "chore", "test", NOW)
    second = items.create("Second", "chore", "test", NOW)
    first_result = tmp_path / "first-result"
    second_result = tmp_path / "second-result"
    ctx = multiprocessing.get_context("spawn")

    _run_pair(
        ctx,
        _duplicate_link_worker,
        (str(state_dir), first.id, str(first_result)),
        (str(state_dir), second.id, str(second_result)),
    )

    assert sorted([first_result.read_text(), second_result.read_text()]) == [
        "duplicate",
        "linked",
    ]
    linked = [item for item in items.list() if item.task_slugs]
    assert len(linked) == 1
    assert linked[0].task_slugs == ["shared-task"]
    assert manager.load().tasks["shared-task"].work_item_id == linked[0].id


def test_concurrent_task_workitem_registration_is_atomic_for_twenty_rounds(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".mothership"
    manager = StateManager(state_dir)
    manager.save(WorkspaceState())
    items = WorkItemStore(state_dir / "workitems")
    first_items = [
        items.create(f"First {index}", "chore", "test", NOW).id
        for index in range(ROUNDS)
    ]
    second_items = [
        items.create(f"Second {index}", "chore", "test", NOW).id
        for index in range(ROUNDS)
    ]
    first_result = tmp_path / "first-registrations.json"
    second_result = tmp_path / "second-registrations.json"
    ctx = multiprocessing.get_context("spawn")

    _run_pair(
        ctx,
        _registration_worker,
        (str(state_dir), first_items, str(first_result)),
        (str(state_dir), second_items, str(second_result)),
    )

    first_outcomes = json.loads(first_result.read_text())
    second_outcomes = json.loads(second_result.read_text())
    state = manager.load()
    assert len(state.tasks) == ROUNDS
    for round_index in range(ROUNDS):
        assert sorted([first_outcomes[round_index], second_outcomes[round_index]]) == [
            "duplicate",
            "registered",
        ]
        task = state.tasks[f"registration-{round_index}"]
        owners = [
            item
            for item in items.list()
            if f"registration-{round_index}" in item.task_slugs
        ]
        assert len(owners) == 1
        assert task.work_item_id == owners[0].id


def test_busy_contention_ends_at_bounded_deadline_with_recovery_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database_module, "BUSY_TIMEOUT_MS", 100)
    state_dir = tmp_path / ".mothership"
    holder = WorkspaceDatabase(state_dir)
    contender = WorkspaceDatabase(state_dir)
    holder.initialize()
    contender.initialize()

    with holder.write(immediate=True):
        started = time.monotonic()
        with pytest.raises(DatabaseBusyError) as error:
            with contender.write(immediate=True):
                pass
        elapsed = time.monotonic() - started

    message = str(error.value)
    assert str(holder.path) in message
    assert "retry the operation" in message
    assert 0.05 <= elapsed < 1.0
