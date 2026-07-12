"""Concurrency regression tests for the per-file stores (MOS-233).

WorkItemStore and MessageStore each do a get()->modify->save() read-modify-write.
Without an exclusive lock spanning that window, two concurrent writers to the
SAME file both load the same JSON, each append their own change, and the second
save() clobbers the first — a lost update.

These mirror tests/core/test_concurrent_mutations.py: real multiprocessing.Process
workers, each with its own store instance on a shared tmp dir, asserting the final
state. Each worker performs several distinct writes to amplify the race window so
the pre-fix version flakes and the post-fix version is deterministic.
"""
from __future__ import annotations

import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mship.core.message_store import MessageStore
from mship.core.workitem_store import WorkItemStore

_BASE = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# MessageStore.append: N processes appending distinct messages to ONE thread.
# ---------------------------------------------------------------------------

def _append_worker(messages_dir_str: str, thread_id: str, worker: int, rounds: int) -> None:
    store = MessageStore(Path(messages_dir_str))
    for j in range(rounds):
        store.append(
            thread_id, "agent", f"w{worker}-m{j}",
            _BASE + timedelta(seconds=worker * 100 + j),
        )


def test_concurrent_appends_to_same_thread_do_not_lose_messages(tmp_path: Path):
    messages_dir = tmp_path / ".mothership" / "messages"
    store = MessageStore(messages_dir)
    thread = store.create_thread(subject="race", text="start", now=_BASE)

    workers, rounds = 8, 4
    procs = [
        multiprocessing.Process(
            target=_append_worker,
            args=(str(messages_dir), thread.id, w, rounds),
        )
        for w in range(workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker crashed (exitcode={p.exitcode})"

    got = store.get(thread.id)
    assert got is not None
    texts = {m.text for m in got.messages if m.role == "agent"}
    expected = {f"w{w}-m{j}" for w in range(workers) for j in range(rounds)}
    missing = expected - texts
    assert not missing, f"lost {len(missing)} appended messages: {sorted(missing)}"
    # initial human message + every agent append survived
    assert len(got.messages) == 1 + workers * rounds


# ---------------------------------------------------------------------------
# WorkItemStore.add_task: N processes adding distinct tasks to ONE work item.
# ---------------------------------------------------------------------------

def _add_task_worker(workitems_dir_str: str, item_id: str, worker: int, rounds: int) -> None:
    store = WorkItemStore(Path(workitems_dir_str))
    for j in range(rounds):
        store.add_task(item_id, f"task-{worker}-{j}")


def test_concurrent_add_task_to_same_item_do_not_lose_updates(tmp_path: Path):
    workitems_dir = tmp_path / ".mothership" / "workitems"
    store = WorkItemStore(workitems_dir)
    item = store.create(title="race", kind="feature", workspace="ws", now=_BASE)

    workers, rounds = 8, 4
    procs = [
        multiprocessing.Process(
            target=_add_task_worker,
            args=(str(workitems_dir), item.id, w, rounds),
        )
        for w in range(workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker crashed (exitcode={p.exitcode})"

    got = store.get(item.id)
    assert got is not None
    slugs = set(got.task_slugs)
    expected = {f"task-{w}-{j}" for w in range(workers) for j in range(rounds)}
    missing = expected - slugs
    assert not missing, f"lost {len(missing)} added task slugs: {sorted(missing)}"
    assert len(got.task_slugs) == workers * rounds


# ---------------------------------------------------------------------------
# WorkItemStore.add_thread: same shape, exercising a second RMW method.
# ---------------------------------------------------------------------------

def _add_thread_worker(workitems_dir_str: str, item_id: str, worker: int, rounds: int) -> None:
    store = WorkItemStore(Path(workitems_dir_str))
    for j in range(rounds):
        store.add_thread(item_id, f"thread-{worker}-{j}")


def test_concurrent_add_thread_to_same_item_do_not_lose_updates(tmp_path: Path):
    workitems_dir = tmp_path / ".mothership" / "workitems"
    store = WorkItemStore(workitems_dir)
    item = store.create(title="race", kind="feature", workspace="ws", now=_BASE)

    workers, rounds = 8, 4
    procs = [
        multiprocessing.Process(
            target=_add_thread_worker,
            args=(str(workitems_dir), item.id, w, rounds),
        )
        for w in range(workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker crashed (exitcode={p.exitcode})"

    got = store.get(item.id)
    assert got is not None
    expected = {f"thread-{w}-{j}" for w in range(workers) for j in range(rounds)}
    missing = expected - set(got.thread_ids)
    assert not missing, f"lost {len(missing)} added thread ids: {sorted(missing)}"
    assert len(got.thread_ids) == workers * rounds
