"""The single approve / request-changes transition, shared by `mship serve`
(core/serve.py), the CLI (cli/spec.py), and the views (core/view/actions.py).

Extracted so the terminal and the phone cannot diverge: every writer performs the
identical guard (approval_blockers + validate_transition) and the identical atomic
store write (SpecStore.save = tempfile + os.replace). Callers own only their own
concerns (HTTP status mapping, CLI output, journal appends, view messaging)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from mship.core.log import LogManager
from mship.core.spec import InvalidTransition, Spec, validate_transition
from mship.core.spec_approve import approval_blockers
from mship.core.spec_store import SpecStore

__all__ = [
    "ApprovalBlocked",
    "InvalidTransition",
    "approve_spec",
    "record_rejection",
    "request_changes_spec",
]


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


def record_rejection(
    log_manager: LogManager, spec_id: str, actor: str, reason: str, now: datetime
) -> None:
    """Append a durable, append-only `rejected` journal event for `spec_id`.

    Unlike `Spec.clarification_reason` (overwritten by the next request-changes
    and nulled by `approve_spec`), the journal is append-only, so this record
    survives later transitions — the durable, queryable rejection history.

    Must be called BEFORE the status transition at every call site (CLI +
    serve): if the append raises, the caller must propagate it (fail loud,
    never swallow — CLAUDE.md) and must not have flipped the spec's status
    yet, so a write failure leaves a consistent, retryable state instead of a
    draft-without-record.

    Raises `ValueError` for an empty/whitespace-only `reason` — every durable
    record must carry real reason text.

    `now` is accepted for interface symmetry with the other transition
    helpers, but `LogManager.append` stamps its own timestamp, so it is not
    threaded through.
    """
    del now
    if not reason or not reason.strip():
        raise ValueError("reason must not be empty")
    log_manager.append(
        spec_id, json.dumps({"actor": actor, "reason": reason}), action="rejected"
    )


def request_changes_spec(
    spec: Spec,
    store: SpecStore,
    reason: str,
    *,
    log_manager: LogManager | None,
    actor: str,
    now: datetime | None = None,
) -> None:
    """needs_review/approved -> draft carrying `reason`. Raises InvalidTransition.

    Writes the durable rejection record itself (record_rejection), in this
    order: reject empty/whitespace reason -> validate_transition -> durable
    record -> status flip -> save. Folding the record into the shared
    transition (rather than leaving it to each caller) is the class fix for
    #458 P1: the CLI, serve, and the TUI views (core/view/actions.py) all
    call this one function, so none of them can transition a spec without
    also logging why — the earlier per-caller convention let the view path
    (`mship view queue/spec/workitem`) silently skip it.
    """
    if not reason or not reason.strip():
        raise ValueError("reason must not be empty")
    validate_transition(spec.status, "draft")
    if now is None:
        now = datetime.now(timezone.utc)
    if log_manager is not None:
        record_rejection(log_manager, spec.id, actor, reason, now)
    spec.status = "draft"
    spec.clarification_reason = reason
    spec.updated_at = now
    store.save(spec)
