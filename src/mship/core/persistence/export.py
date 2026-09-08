from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from mship.core.persistence.backend import StorageBackend, detect_backend
from mship.core.persistence.database import WorkspaceDatabase
from mship.core.state import StateManager
from mship.core.workitem_store import WorkItemStore


@dataclass(frozen=True)
class StorageStatus:
    backend: StorageBackend
    database_path: Path
    current_revision: str | None
    head_revision: str
    migration_required: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend.value,
            "database_path": str(self.database_path),
            "current_revision": self.current_revision,
            "head_revision": self.head_revision,
            "migration_required": self.migration_required,
        }


def storage_status(state_dir: Path) -> StorageStatus:
    """Inspect the selected state backend without creating or migrating it."""
    state_dir = Path(state_dir)
    backend = detect_backend(state_dir)
    database = WorkspaceDatabase(state_dir)
    current_revision = (
        database.current_revision() if backend is StorageBackend.SQLITE else None
    )
    head_revision = database.head_revision()
    return StorageStatus(
        backend=backend,
        database_path=database.path,
        current_revision=current_revision,
        head_revision=head_revision,
        migration_required=(
            backend is StorageBackend.LEGACY
            or (backend is StorageBackend.SQLITE and current_revision != head_revision)
        ),
    )


def _export_payload(state_dir: Path) -> dict[str, list[dict[str, object]]]:
    state = StateManager(state_dir).load()
    items = WorkItemStore(state_dir / "workitems").list(include_archived=True)
    tasks = []
    for slug in sorted(state.tasks):
        task = state.tasks[slug].model_dump(mode="json")
        task["passive_repos"] = sorted(task["passive_repos"])
        tasks.append(task)
    return {
        "tasks": tasks,
        "work_items": [
            item.model_dump(mode="json")
            for item in sorted(items, key=lambda value: value.id)
        ],
    }


def export_state(
    state_dir: Path,
    *,
    format: Literal["json", "yaml"] | str = "json",
) -> str:
    """Render only Task and WorkItem records in a deterministic text format."""
    if format not in {"json", "yaml"}:
        raise ValueError("unsupported state export format; use json or yaml")
    payload = _export_payload(Path(state_dir))
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=True)
