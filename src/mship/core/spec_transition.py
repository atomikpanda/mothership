"""The single approve / request-changes transition, shared by `mship serve`
(core/serve.py), the CLI (cli/spec.py), and the views (core/view/actions.py).

Extracted so the terminal and the phone cannot diverge: every writer performs the
identical guard and reloads, mutates, and persists under the same per-spec lock
as draft apply. Callers own only their own concerns (HTTP status mapping, CLI
output, and view messaging)."""
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


def approve_spec(spec: Spec, store: SpecStore, *, bypass_gate: bool = False) -> Spec:
    """Approve the current revision under the same lock as draft apply."""
    with store.locked(spec.id) as artifact:
        if artifact is None:
            raise ValueError(f"No spec with id {spec.id!r}.")
        current = artifact.spec
        if not bypass_gate:
            blockers = approval_blockers(current)
            if blockers:
                raise ApprovalBlocked(blockers)
        validate_transition(current.status, "approved")
        current.status = "approved"
        current.clarification_reason = None
        current.updated_at = datetime.now(timezone.utc)
        store.save_while_locked(current, artifact)
        return current


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
) -> Spec:
    """Return the current spec to draft under the same lock as draft apply.

    The durable rejection record is appended before the in-lock status write, so
    an append failure leaves the persisted spec unchanged.
    """
    if not reason or not reason.strip():
        raise ValueError("reason must not be empty")
    if now is None:
        now = datetime.now(timezone.utc)
    with store.locked(spec.id) as artifact:
        if artifact is None:
            raise ValueError(f"No spec with id {spec.id!r}.")
        current = artifact.spec
        validate_transition(current.status, "draft")
        if log_manager is not None:
            record_rejection(log_manager, current.id, actor, reason, now)
        current.status = "draft"
        current.clarification_reason = reason
        current.updated_at = now
        store.save_while_locked(current, artifact)
        return current
