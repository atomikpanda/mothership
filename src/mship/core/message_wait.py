"""Poll-until-change over the message mailbox: the shared primitive behind the
agent-side `mship inbox wait` long-poll and the serve-side `GET /threads?wait=1`.

`changed_since` is pure (the tested core). `wait_for_change` is a thin sync loop
with injectable clock/sleep so tests never sleep for real. The cursor is an
ephemeral high-water timestamp the caller threads through — there is no on-disk
cursor. See spec idle-session-wake-serve-long-poll-for-mailbox-239-slice-2.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


def changed_since(threads, since: datetime):
    """Return (changed, cursor): threads whose updated_at is strictly after
    `since`, and the high-water cursor = max(updated_at across all threads, since)
    (so the cursor never moves backwards)."""
    changed = [t for t in threads if t.updated_at > since]
    cursor = max([since, *(t.updated_at for t in threads)])
    return changed, cursor


def stamp_agent_seen(store, threads, now: datetime) -> None:
    """Advance the AGENT read cursor to `now` on every surfaced thread whose latest message is human
    (`awaiting_reply`) — the agent is now SEEING it, which is the "Read" signal (#345). Shared by
    `mship inbox wait` and `_drain`, the two places that surface human messages to the agent. Only
    awaiting_reply threads (not `awaiting_agent_event`-only ones). Best-effort per thread — a stamp
    failure (e.g. a thread deleted mid-flight) must never propagate to the caller."""
    for t in threads:
        if getattr(t, "awaiting_reply", False):
            try:
                store.mark_agent_seen(t.id, now)
            except Exception:
                pass


@dataclass(frozen=True)
class WaitResult:
    threads: list
    cursor: datetime
    timed_out: bool


def wait_for_change(
    load_fn: Callable[[], list],
    since: datetime,
    timeout: float,
    *,
    predicate: Callable[[object], bool] | None = None,
    now_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    interval: float = 1.0,
) -> WaitResult:
    """Block until `load_fn()` yields a thread changed since `since` that also
    satisfies `predicate` (default: any change), or `timeout` seconds elapse.
    The cursor always advances to the latest seen updated_at, even on timeout."""
    deadline = now_fn() + timeout
    while True:
        changed, cursor = changed_since(load_fn(), since)
        hits = [t for t in changed if predicate(t)] if predicate else changed
        if hits:
            return WaitResult(threads=hits, cursor=cursor, timed_out=False)
        remaining = deadline - now_fn()
        if remaining <= 0:
            return WaitResult(threads=[], cursor=cursor, timed_out=True)
        sleep_fn(min(interval, remaining))
