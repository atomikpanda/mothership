"""Shared reclaim semantics for single-holder leases/claims.

Both the in-process inbox-listener lease (`core/inbox_lease.py`, keyed on a pid
with an `os.kill` liveness probe) and the git-backed run-claim
(`core/run_state.py`, keyed on an opaque holder token with no probe — an ephemeral
cloud run can't be `os.kill`-ed) answer the same question: *may I take this over?*

A holder is reclaimable when it is unheld, already ours, its heartbeat has gone
stale past the TTL, or a supplied liveness probe says the holder is gone. Keeping
the rule in one place means the two stores can't drift apart.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable


def is_reclaimable(
    *,
    holder_identity: object | None,
    heartbeat_at: datetime | None,
    me: object,
    now: datetime,
    ttl_seconds: float,
    is_alive: Callable[[object], bool] | None = None,
) -> bool:
    """True if the caller (`me`) may take over the lease/claim.

    Reclaimable when any of:
      - unheld (`holder_identity is None`), or
      - we already hold it (`holder_identity == me`), or
      - the heartbeat is missing or older than `ttl_seconds` (stale → holder
        crashed/exited/wedged), or
      - `is_alive(holder_identity)` is provided and reports the holder is gone.

    A live, fresh, foreign holder is NOT reclaimable → the caller must stand down.
    When `is_alive` is None (e.g. a remote token that can't be probed), liveness
    collapses onto the heartbeat TTL alone.
    """
    if holder_identity is None or holder_identity == me:
        return True
    if heartbeat_at is None:
        return True
    if (now - heartbeat_at).total_seconds() > ttl_seconds:
        return True  # heartbeat went stale → holder is gone or wedged
    if is_alive is not None:
        return not is_alive(holder_identity)
    return False
