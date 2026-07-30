"""The single approve-a-plan-assumption-flag mutation, shared by the CLI verb
(`mship plan assumptions approve`) and the serve endpoint
(`POST /plan-assumptions/{slug}/approve`) so they cannot drift — mirrors how
spec approve/verdict live in core/spec_transition.py rather than inline in serve.
"""
from __future__ import annotations

from mship.core.plan import _normalize_axis
from mship.core.plan_check import PlanCheckResult, PlanCheckStore


class NoStoredCheck(Exception):
    """No plan-check result on record for the task — nothing to approve."""


class UnknownAxis(Exception):
    """No flag for the given axis (it is not a current row, or was dispositioned
    covered/N-A so it never produced a flag)."""


def approve_flag(
    store: PlanCheckStore, slug: str, axis: str, reason: str | None, approved_by: str,
) -> PlanCheckResult:
    """Approve the pending flag for `axis` on `slug`. Idempotent: an already-approved
    axis returns the stored result unchanged. Raises NoStoredCheck / UnknownAxis.
    The whole read-modify-write runs under the per-task exclusive lock so a
    concurrent checker refresh or double-approve cannot clobber it."""
    norm = _normalize_axis(axis)
    with store.transaction(slug):
        stored = store.get(slug)
        if stored is None:
            raise NoStoredCheck(slug)
        match = next((f for f in stored.flags if _normalize_axis(f.axis) == norm), None)
        if match is None:
            raise UnknownAxis(axis)
        if match.approved:
            return stored  # idempotent no-op
        match.approved = True
        match.approved_reason = reason
        match.approved_by = approved_by
        store.save(stored)
        return stored
