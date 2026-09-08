from __future__ import annotations

from datetime import timezone
from enum import StrEnum
from pathlib import Path

import yaml

from mship.core.persistence.database import DB_FILENAME
from mship.core.state import WorkspaceState
from mship.core.workitem import WorkItem

LEGACY_STATE_FILENAME = "state.yaml"
LEGACY_WORKITEMS_DIRNAME = "workitems"


class StorageBackend(StrEnum):
    EMPTY = "empty"
    LEGACY = "legacy"
    SQLITE = "sqlite"


def detect_backend(state_dir: Path) -> StorageBackend:
    state_dir = Path(state_dir)
    if (state_dir / DB_FILENAME).is_file():
        return StorageBackend.SQLITE
    if (state_dir / LEGACY_STATE_FILENAME).is_file():
        return StorageBackend.LEGACY
    workitems_dir = state_dir / LEGACY_WORKITEMS_DIRNAME
    if workitems_dir.is_dir() and next(workitems_dir.glob("*.json"), None) is not None:
        return StorageBackend.LEGACY
    return StorageBackend.EMPTY


def load_legacy_state(state_dir: Path) -> WorkspaceState:
    state_file = Path(state_dir) / LEGACY_STATE_FILENAME
    if not state_file.is_file():
        return WorkspaceState()
    with state_file.open() as stream:
        raw = yaml.safe_load(stream)
    if raw is None:
        return WorkspaceState()
    return WorkspaceState.model_validate(raw)


def _validate_legacy_workitem_id(item_id: str) -> None:
    if (
        not item_id
        or "/" in item_id
        or "\\" in item_id
        or item_id in (".", "..")
        or item_id.startswith(".")
    ):
        raise ValueError(f"unsafe work item id: {item_id!r}")


def _contained_legacy_workitem_path(
    workitems_dir: Path,
    discovered: Path,
) -> Path:
    directory = Path(workitems_dir).resolve()
    path = discovered.resolve()
    if path.parent != directory:
        raise ValueError(f"unsafe work item id: {discovered.stem!r}")
    return path


def _legacy_workitem_entries(workitems_dir: Path) -> list[tuple[str, Path]]:
    directory = Path(workitems_dir)
    if not directory.is_dir():
        return []
    return [
        (discovered.stem, _contained_legacy_workitem_path(directory, discovered))
        for discovered in directory.glob("*.json")
    ]


def get_legacy_workitem(workitems_dir: Path, item_id: str) -> WorkItem | None:
    _validate_legacy_workitem_id(item_id)
    directory = Path(workitems_dir)
    if not directory.is_dir():
        return None
    for discovered in directory.glob("*.json"):
        if discovered.stem != item_id:
            continue
        path = _contained_legacy_workitem_path(directory, discovered)
        if path.is_file():
            return WorkItem.model_validate_json(path.read_text())
        return None
    return None


def list_legacy_workitems(
    workitems_dir: Path,
    *,
    include_archived: bool = False,
    tolerant: bool = False,
) -> tuple[list[WorkItem], bool]:
    directory = Path(workitems_dir)
    if not directory.is_dir():
        return [], False
    items: list[WorkItem] = []
    uncertain = False
    for discovered in directory.glob("*.json"):
        try:
            path = _contained_legacy_workitem_path(
                directory,
                discovered,
            )
            items.append(WorkItem.model_validate_json(path.read_text()))
        except Exception:
            if not tolerant:
                raise
            uncertain = True
    if not include_archived:
        items = [item for item in items if not item.archived]
    return (
        sorted(
            items,
            key=lambda item: (
                item.updated_at.replace(tzinfo=timezone.utc)
                if item.updated_at.tzinfo is None
                else item.updated_at
            ),
            reverse=True,
        ),
        uncertain,
    )
