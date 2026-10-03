from __future__ import annotations

import threading

import anyio
import pytest

from mship.core.async_runtime import LANE_CAPACITIES, _limiter_for, run_sync


async def _wait_until(event: threading.Event) -> None:
    with anyio.fail_after(2):
        while not event.is_set():
            await anyio.sleep(0)


@pytest.mark.parametrize("lane", ["tunnel", "registry", "pr_watch", "mailbox"])
def test_same_lane_calls_do_not_exceed_declared_capacity(lane):
    """Removing a lane limiter would let every blocked call enter together."""
    capacity = LANE_CAPACITIES[lane]
    saturated = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0

    def blocking_call():
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == capacity:
                saturated.set()
        try:
            assert release.wait(2), "test did not release the offload lane"
        finally:
            with lock:
                active -= 1

    async def scenario():
        async with anyio.create_task_group() as task_group:
            for _ in range(capacity + 1):
                task_group.start_soon(run_sync, lane, blocking_call)
            await _wait_until(saturated)
            for _ in range(10):
                await anyio.sleep(0)
            assert max_active == capacity
            release.set()

    anyio.run(scenario, backend="asyncio")


def test_saturated_mailbox_lane_does_not_block_registry_or_pr_watch():
    """Sharing one limiter across work classes would starve unrelated work."""
    saturated = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    registry_finished = threading.Event()
    watcher_finished = threading.Event()

    def blocking_mailbox_call():
        nonlocal active
        with lock:
            active += 1
            if active == LANE_CAPACITIES["mailbox"]:
                saturated.set()
        try:
            assert release.wait(2), "test did not release the mailbox lane"
        finally:
            with lock:
                active -= 1

    async def scenario():
        async with anyio.create_task_group() as task_group:
            for _ in range(LANE_CAPACITIES["mailbox"]):
                task_group.start_soon(run_sync, "mailbox", blocking_mailbox_call)
            await _wait_until(saturated)
            with anyio.fail_after(1):
                await run_sync("registry", registry_finished.set)
                await run_sync("pr_watch", watcher_finished.set)
            release.set()

    anyio.run(scenario, backend="asyncio")

    assert registry_finished.is_set()
    assert watcher_finished.is_set()


def test_lane_limiters_are_shared_within_but_not_between_anyio_runs():
    """A backend-bound limiter reused by a later event loop would be invalid."""
    async def collect_limiters():
        limiters = [_limiter_for("registry")]

        async def collect():
            limiters.append(_limiter_for("registry"))

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(collect)
            task_group.start_soon(collect)
        return limiters

    first_run = anyio.run(collect_limiters, backend="asyncio")
    second_run = anyio.run(collect_limiters, backend="asyncio")

    assert all(limiter is first_run[0] for limiter in first_run)
    assert all(limiter is second_run[0] for limiter in second_run)
    assert first_run[0] is not second_run[0]


def test_cancellation_waits_for_inflight_lane_work_to_finish():
    """Abandoning a cancelled worker could outlive daemon-owned state."""
    entered = threading.Event()
    release = threading.Event()

    def blocking_call():
        entered.set()
        assert release.wait(2), "test did not release the offload"

    async def scenario():
        finished = anyio.Event()
        scope = anyio.CancelScope()

        async def call():
            with scope:
                await run_sync("registry", blocking_call)
            finished.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(call)
            await _wait_until(entered)
            scope.cancel()
            for _ in range(10):
                await anyio.sleep(0)
            assert not finished.is_set()
            release.set()
            with anyio.fail_after(1):
                await finished.wait()

    anyio.run(scenario, backend="asyncio")
