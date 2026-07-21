"""The single approve / request-changes transition, shared by `mship serve`
(core/serve.py), the CLI (cli/spec.py), and the views (core/view/actions.py).

Extracted so the terminal and the phone cannot diverge: every writer performs the
identical guard (approval_blockers + validate_transition) and the identical atomic
store write (SpecStore.save = tempfile + os.replace). Callers own only their own
concerns (HTTP status mapping, CLI output, journal appends, view messaging)."""
from __future__ import annotations

from datetime import datetime, timezone

from mship.core.spec import InvalidTransition, Spec, validate_transition
from mship.core.spec_approve import approval_blockers
from mship.core.spec_store import SpecStore

__all__ = ["ApprovalBlocked", "InvalidTransition", "approve_spec", "request_changes_spec"]


class ApprovalBlocked(Exception):
    """Approval gate not met (unapproved criteria / unanswered questions / prose)."""

    def __init__(self, blockers: list[str]) -> None:
        super().__init__("; ".join(blockers))
        self.blockers = list(blockers)


def approve_spec(spec: Spec, store: SpecStore, *, bypass_gate: bool = False) -> None:
    """needs_review -> approved. Raises ApprovalBlocked (gate) or InvalidTransition."""
    if not bypass_gate:
        blockers = approval_blockers(spec)
        if blockers:
            raise ApprovalBlocked(blockers)
    validate_transition(spec.status, "approved")
    spec.status = "approved"
    spec.clarification_reason = None
    spec.updated_at = datetime.now(timezone.utc)
    store.save(spec)


def request_changes_spec(spec: Spec, store: SpecStore, reason: str) -> None:
    """needs_review/approved -> draft carrying `reason`. Raises InvalidTransition."""
    validate_transition(spec.status, "draft")
    spec.status = "draft"
    spec.clarification_reason = reason
    spec.updated_at = datetime.now(timezone.utc)
    store.save(spec)
