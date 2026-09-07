from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic_core import to_jsonable_python

from mship.core.workitem import WorkItem


class PersistenceError(RuntimeError):
    """Base class for stable persistence-boundary failures."""


class PersistenceDecodeError(PersistenceError):
    """A stored entity could not be restored through its Pydantic contract."""

    def __init__(self, table: str, entity_id: str, cause: Exception) -> None:
        self.table = table
        self.entity_id = entity_id
        super().__init__(
            f"could not decode {table} entity {entity_id!r}: {cause}"
        )


class ConcurrentUpdateError(PersistenceError):
    """An optimistic revision no longer matches the persisted entity."""

    def __init__(
        self,
        table: str,
        entity_id: str,
        expected_revision: int,
    ) -> None:
        self.table = table
        self.entity_id = entity_id
        self.expected_revision = expected_revision
        super().__init__(
            f"concurrent update for {table} entity {entity_id!r}: "
            f"expected revision {expected_revision}"
        )


def encode_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def decode_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    decoded = datetime.fromisoformat(value)
    if decoded.tzinfo is None:
        decoded = decoded.replace(tzinfo=timezone.utc)
    return decoded.astimezone(timezone.utc)


def encode_path(value: Path | None) -> str | None:
    return None if value is None else str(value)


def encode_json(value: object) -> str:
    return json.dumps(
        to_jsonable_python(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def decode_json(value: str) -> object:
    return json.loads(value)


def workitem_extras(item: WorkItem) -> dict[str, object]:
    return dict(item.__pydantic_extra__ or {})
