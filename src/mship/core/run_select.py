"""Pure selection of the next runnable WorkItem for the unattended runner.

Eligibility: item is `unattended`, its derived/override phase is `ready`, it has a
spec that is `approved`, it is not currently claimed, and it is not blocked (a
prior run bailed on it). Ordered oldest-first so the backlog drains FIFO. No I/O —
callers supply loaded state. #unattended-runner
"""
from __future__ import annotations

from dataclasses import dataclass

from mship.core.view.workitem_index import compute_phase


@dataclass(frozen=True)
class Candidate:
    item: object  # WorkItem


def _phase(item, specs_by_id, tasks_by_slug) -> str:
    # Finding 3 (cleared-override skips runner): resolve the item's DERIVED phase via
    # compute_phase (spec + tasks), not just phase_override. Clearing an override
    # (Reopen) leaves phase_override=None; an item whose derived phase is `ready`
    # (e.g. an approved spec) must stay selectable — reading only phase_override would
    # see `inbox` and hide it from run-next forever. compute_phase still honours an
    # explicit override when one is set. This resolves the documented v1 shortcut.
    spec = specs_by_id.get(item.spec_id) if item.spec_id else None
    tasks = [tasks_by_slug[s] for s in item.task_slugs if s in tasks_by_slug]
    return compute_phase(item, spec, tasks)


def select_runnable(
    items,
    spec_approved: dict[str, bool],
    claimed: set[str],
    blocked: set[str] = frozenset(),
    specs_by_id: dict | None = None,
    tasks_by_slug: dict | None = None,
) -> list[Candidate]:
    """``blocked`` is the set of item-ids whose linked task(s) carry a
    ``blocked_reason`` (a prior run bailed). Excluding them stops the runner from
    re-picking a bailed item on every tick — a human/decision must unblock it
    first. See FIX#1 (couples with the cross-process bail release, FIX#2).

    ``specs_by_id`` / ``tasks_by_slug`` supply the child state ``compute_phase`` needs
    to resolve each item's derived phase (Finding 3); default-empty so a caller that
    only sets ``phase_override`` still selects correctly."""
    specs_by_id = specs_by_id or {}
    tasks_by_slug = tasks_by_slug or {}
    eligible = [
        it for it in items
        if getattr(it, "unattended", False)
        and _phase(it, specs_by_id, tasks_by_slug) == "ready"
        and it.spec_id is not None
        and spec_approved.get(it.spec_id, False)
        and it.id not in claimed
        and it.id not in blocked
    ]
    eligible.sort(key=lambda it: it.created_at)
    return [Candidate(item=it) for it in eligible]
