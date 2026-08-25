from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from mship.core.message import Thread
    from mship.core.spec import Spec


InboxState = Literal["active", "archived"]
InboxAction = Literal["archive", "restore", "pin", "unpin"]
InboxArchiveReason = Literal[
    "manual", "linked_terminal", "inactive_unlinked",
    "implemented", "lifecycle_archived",
]

_SEVEN_DAYS = timedelta(days=7)
_ACTIONS = frozenset(("archive", "restore", "pin", "unpin"))


class InboxMetadata(BaseModel):
    pinned: bool = False
    manual_archived: bool = False
    restored_at: datetime | None = None
    last_mutated_at: datetime | None = None
    mutation_ids: dict[str, InboxAction] = Field(default_factory=dict)


class InboxClassification(BaseModel):
    state: InboxState
    archive_reason: InboxArchiveReason | None = None


def _restore_is_effective(metadata: InboxMetadata, now: datetime) -> bool:
    return metadata.restored_at is not None and now - metadata.restored_at < _SEVEN_DAYS


def classify_thread(
    thread: Thread,
    *,
    linked: bool,
    linked_terminal: bool,
    now: datetime,
) -> InboxClassification:
    """Classify a thread without changing its inbox or domain state."""
    metadata = thread.inbox
    if metadata.pinned or thread.needs_you or thread.needs_decision:
        return InboxClassification(state="active")
    if metadata.manual_archived:
        return InboxClassification(state="archived", archive_reason="manual")
    if _restore_is_effective(metadata, now):
        return InboxClassification(state="active")
    if linked and linked_terminal:
        return InboxClassification(state="archived", archive_reason="linked_terminal")
    if not linked and now - thread.updated_at >= _SEVEN_DAYS:
        return InboxClassification(state="archived", archive_reason="inactive_unlinked")
    return InboxClassification(state="active")


def classify_spec(spec: Spec, *, now: datetime) -> InboxClassification:
    """Classify a spec without changing its inbox or lifecycle state."""
    metadata = spec.inbox
    if metadata.pinned:
        return InboxClassification(state="active")
    if metadata.manual_archived:
        return InboxClassification(state="archived", archive_reason="manual")
    if _restore_is_effective(metadata, now):
        return InboxClassification(state="active")
    if spec.status == "archived":
        return InboxClassification(state="archived", archive_reason="lifecycle_archived")
    if spec.status == "implemented" and now - spec.updated_at >= _SEVEN_DAYS:
        return InboxClassification(state="archived", archive_reason="implemented")
    return InboxClassification(state="active")


def apply_inbox_action(
    metadata: InboxMetadata,
    action: InboxAction,
    mutation_id: str,
    now: datetime,
) -> bool:
    """Mutate in place; return False for an identical retry."""
    if action not in _ACTIONS:
        raise ValueError(f"unknown inbox action: {action!r}")
    previous = metadata.mutation_ids.get(mutation_id)
    if previous is not None:
        if previous != action:
            raise ValueError(
                f"mutation id {mutation_id!r} already used for {previous!r}, not {action!r}"
            )
        return False
    metadata.mutation_ids[mutation_id] = action
    if action == "archive":
        metadata.manual_archived = True
    elif action == "restore":
        metadata.manual_archived = False
        metadata.restored_at = now
    elif action == "pin":
        metadata.pinned = True
    else:
        metadata.pinned = False
    metadata.last_mutated_at = max(
        now,
        metadata.last_mutated_at + timedelta(microseconds=1)
        if metadata.last_mutated_at is not None else now,
    )
    return True
