"""Thin view-facing wrapper over core.spec_transition. Translates the shared
approve/request-changes transition into a render-ready ActionOutcome: verifies the
target is needs_review, no-ops (ok=False) with a visible message otherwise, and
surfaces the approval gate. No Textual — unit-testable directly."""
from __future__ import annotations

from dataclasses import dataclass

from mship.core.spec import InvalidTransition
from mship.core.spec_store import SpecStore
from mship.core.spec_transition import ApprovalBlocked, approve_spec, request_changes_spec


@dataclass(frozen=True)
class ActionOutcome:
    ok: bool
    message: str
    new_status: str | None = None


def _load_reviewable(store: SpecStore, spec_id: str | None):
    if not spec_id:
        return None, ActionOutcome(False, "No spec on this row.")
    spec = store.find_by_id(spec_id)
    if spec is None:
        return None, ActionOutcome(False, f"Spec {spec_id} not found.")
    if spec.status != "needs_review":
        return None, ActionOutcome(False, f"{spec_id} is {spec.status}, not awaiting review.")
    return spec, None


def approve_spec_by_id(store: SpecStore, spec_id: str | None) -> ActionOutcome:
    spec, bail = _load_reviewable(store, spec_id)
    if bail is not None:
        return bail
    try:
        approve_spec(spec, store)
    except ApprovalBlocked as e:
        return ActionOutcome(False, f"Cannot approve {spec_id}: {'; '.join(e.blockers)}")
    except InvalidTransition as e:
        return ActionOutcome(False, str(e))
    return ActionOutcome(True, f"Approved {spec_id}.", new_status="approved")


def request_changes_by_id(store: SpecStore, spec_id: str | None, reason: str) -> ActionOutcome:
    spec, bail = _load_reviewable(store, spec_id)
    if bail is not None:
        return bail
    reason = (reason or "").strip()
    if not reason:
        return ActionOutcome(False, "Request-changes needs a reason.")
    try:
        request_changes_spec(spec, store, reason)
    except InvalidTransition as e:
        return ActionOutcome(False, str(e))
    return ActionOutcome(True, f"Requested changes on {spec_id}.", new_status="draft")
