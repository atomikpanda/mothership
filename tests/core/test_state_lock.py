import multiprocessing
from pathlib import Path

import pytest

from mship.core.state import StateManager, WorkspaceState, Task
from datetime import datetime, timezone


def _append_task(state_dir_str: str, slug: str):
    """Subprocess body: open its own StateManager and mutate to add a task."""
    state_dir = Path(state_dir_str)
    sm = StateManager(state_dir)

    def _mutate(s: WorkspaceState):
        s.tasks[slug] = Task(
            slug=slug,
            description=f"from {slug}",
            phase="plan",
            created_at=datetime(2026, 4, 16, tzinfo=timezone.utc),
            affected_repos=[],
            branch=f"feat/{slug}",
        )
    sm.mutate(_mutate)


def test_mutate_serializes_concurrent_writers(tmp_path: Path):
    sm = StateManager(tmp_path)
    sm.save(WorkspaceState())

    procs = [
        multiprocessing.Process(target=_append_task, args=(str(tmp_path), f"t{i}"))
        for i in range(5)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    state = sm.load()
    assert set(state.tasks.keys()) == {"t0", "t1", "t2", "t3", "t4"}


def test_shared_read_does_not_block_readers(tmp_path: Path):
    """Two concurrent load() calls should both return quickly — shared locks don't block each other."""
    import threading
    import time

    sm = StateManager(tmp_path)
    sm.save(WorkspaceState(tasks={"x": Task(
        slug="x", description="d", phase="plan",
        created_at=datetime(2026, 4, 16, tzinfo=timezone.utc),
        affected_repos=[], branch="feat/x",
    )}))

    results = []

    def _reader():
        t0 = time.monotonic()
        for _ in range(50):
            sm.load()
        results.append(time.monotonic() - t0)

    threads = [threading.Thread(target=_reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # If shared locks blocked each other, total time scales with thread count.
    # Assert each reader finished in under 2s — generous headroom for CI.
    assert all(r < 2.0 for r in results), f"readers took too long: {results}"
