"""The single approve / request-changes transition, shared by `mship serve`
(core/serve.py), the CLI (cli/spec.py), and the views (core/view/actions.py).

Extracted so the terminal and the phone cannot diverge: every writer performs the
identical guard (approval_blockers + validate_transition) and the identical atomic
store write (SpecStore.save = tempfile + os.replace). Callers own only their own
concerns (HTTP status mapping, CLI output, journal appends, view messaging)."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
from datetime import datetime, timedelta, timezone

from mship.core.log import LogManager
from mship.core.spec import InvalidTransition, Spec, validate_transition
from mship.core.spec_approve import approval_blockers
from mship.core.spec_storage import SpecLocked
from mship.core.spec_store import SpecParseError, SpecStore

__all__ = [
    "ApprovalBlocked",
    "InvalidTransition",
    "SpecRevisionConflict",
    "approve_spec",
    "record_rejection",
    "request_changes_spec",
]


class ApprovalBlocked(Exception):
    """Approval gate not met (unapproved criteria / unanswered questions / prose)."""

    def __init__(self, blockers: list[str]) -> None:
        super().__init__("; ".join(blockers))
        self.blockers = list(blockers)


class SpecRevisionConflict(Exception):
    """The persisted spec no longer has the revision the caller loaded."""

    def __init__(
        self,
        spec_id: str,
        expected_updated_at: datetime,
        current_updated_at: datetime | None,
    ) -> None:
        super().__init__(f"spec revision conflict for {spec_id!r}; reload and retry")
        self.spec_id = spec_id
        self.expected_updated_at = expected_updated_at
        self.current_updated_at = current_updated_at


def _assert_current_revision(spec: Spec, current: Spec | None) -> None:
    current_updated_at = current.updated_at if current is not None else None
    if (
        current is None
        or spec.model_dump(exclude={"inbox"}) != current.model_dump(exclude={"inbox"})
    ):
        raise SpecRevisionConflict(spec.id, spec.updated_at, current_updated_at)


def _transition_timestamp(expected_updated_at: datetime, proposed_at: datetime) -> datetime:
    """Return a revision timestamp that is strictly newer than the expected one."""
    if expected_updated_at.tzinfo is None:
        proposed_at = proposed_at.replace(tzinfo=None)
    elif proposed_at.tzinfo is None:
        proposed_at = proposed_at.replace(tzinfo=expected_updated_at.tzinfo)
    else:
        proposed_at = proposed_at.astimezone(expected_updated_at.tzinfo)
    if proposed_at > expected_updated_at:
        return proposed_at
    return expected_updated_at + timedelta.resolution


@contextmanager
def _locked_current_revision(spec: Spec, store: SpecStore):
    with ExitStack() as stack:
        try:
            artifact = stack.enter_context(store.locked(spec.id))
        except (SpecLocked, SpecParseError) as exc:
            raise SpecRevisionConflict(spec.id, spec.updated_at, None) from exc
        _assert_current_revision(spec, artifact.spec if artifact is not None else None)
        yield artifact


def approve_spec(spec: Spec, store: SpecStore, *, bypass_gate: bool = False) -> None:
    """needs_review -> approved. Raises ApprovalBlocked (gate) or InvalidTransition."""
    if not bypass_gate:
        blockers = approval_blockers(spec)
        if blockers:
            raise ApprovalBlocked(blockers)
    validate_transition(spec.status, "approved")
    with _locked_current_revision(spec, store) as artifact:
        spec.status = "approved"
        spec.clarification_reason = None
        spec.updated_at = _transition_timestamp(
            spec.updated_at, datetime.now(timezone.utc)
        )
        store.save_while_locked(spec, artifact)


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
    with _locked_current_revision(spec, store) as artifact:
        if log_manager is not None:
            record_rejection(log_manager, spec.id, actor, reason, now)
        spec.status = "draft"
        spec.clarification_reason = reason
        spec.updated_at = _transition_timestamp(spec.updated_at, now)
        store.save_while_locked(spec, artifact)
