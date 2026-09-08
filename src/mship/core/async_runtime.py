"""Run-local thread offload lanes for daemon-owned blocking work."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, ParamSpec, TypeVar

import anyio
from anyio.lowlevel import RunVar


OffloadLane = Literal["tunnel", "registry", "pr_watch", "mailbox"]

# Separate limiters keep an occupied work class from starving another.  Values
# are deliberately explicit even where equal so each lane can be tuned without
# changing the others' concurrency contract.
LANE_CAPACITIES: dict[OffloadLane, int] = {
    "tunnel": 1,
    "registry": 1,
    "pr_watch": 1,
    "mailbox": 4,
}

_limiters: RunVar[dict[OffloadLane, anyio.CapacityLimiter]] = RunVar(
    "mship_offload_limiters"
)
_P = ParamSpec("_P")
_T = TypeVar("_T")


def _limiter_for(lane: OffloadLane) -> anyio.CapacityLimiter:
    try:
        limiters = _limiters.get()
    except LookupError:
        limiters = {
            name: anyio.CapacityLimiter(capacity)
            for name, capacity in LANE_CAPACITIES.items()
        }
        _limiters.set(limiters)
    return limiters[lane]


async def run_sync(
    lane: OffloadLane,
    func: Callable[_P, _T],
    *args: _P.args,
) -> _T:
    """Run ``func`` in its named lane and join it if the caller is cancelled.

    Daemon offloads may mutate shared stores or own subprocess transitions, so
    every lane uses the same non-abandoning cancellation policy: cancellation
    waits for an in-flight worker to return before control leaves this await.
    """
    return await anyio.to_thread.run_sync(
        func,
        *args,
        abandon_on_cancel=False,
        limiter=_limiter_for(lane),
    )
