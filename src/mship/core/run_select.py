"""Pure selection of the next runnable WorkItem for the unattended runner.

Eligibility: item is `unattended`, its derived/override phase is `ready`, it has a
spec that is `approved`, it is not currently claimed, and it is not blocked (a
prior run bailed on it). Ordered oldest-first so the backlog drains FIFO. No I/O —
callers supply loaded state. #unattended-runner
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    item: object  # WorkItem


def _phase(item) -> str:
    # v1: the selector reads the item's phase_override; when the derived-phase
    # index lands, swap this for the computed phase. Spec q: ready = derived ready.
    return item.phase_override or "inbox"


def select_runnable(
    items,
    spec_approved: dict[str, bool],
    claimed: set[str],
    blocked: set[str] = frozenset(),
) -> list[Candidate]:
    """``blocked`` is the set of item-ids whose linked task(s) carry a
    ``blocked_reason`` (a prior run bailed). Excluding them stops the runner from
    re-picking a bailed item on every tick — a human/decision must unblock it
    first. See FIX#1 (couples with the cross-process bail release, FIX#2)."""
    eligible = [
        it for it in items
        if getattr(it, "unattended", False)
        and _phase(it) == "ready"
        and it.spec_id is not None
        and spec_approved.get(it.spec_id, False)
        and it.id not in claimed
        and it.id not in blocked
    ]
    eligible.sort(key=lambda it: it.created_at)
    return [Candidate(item=it) for it in eligible]
