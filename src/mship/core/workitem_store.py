from __future__ import annotations

import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from mship.core.state import StateManager
from mship.core.workitem import ExternalLink, Kind, Phase, WorkItem


def _new_id(now: datetime) -> str:
    return f"wi-{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"


class WorkItemStore:
    """Filesystem registry for work items: one JSON file per item."""

    def __init__(self, workitems_dir: Path) -> None:
        self._dir = Path(workitems_dir)

    def _path(self, item_id: str) -> Path:
        if (not item_id or "/" in item_id or "\\" in item_id
                or item_id in (".", "..") or item_id.startswith(".")):
            raise ValueError(f"unsafe work item id: {item_id!r}")
        return self._dir / f"{item_id}.json"

    def save(self, item: WorkItem) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(item.id)
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".json.tmp")
        try:
            with open(fd, "w") as f:
                f.write(item.model_dump_json(indent=2))
            Path(tmp).replace(path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def get(self, item_id: str) -> WorkItem | None:
        path = self._path(item_id)
        if not path.is_file():
            return None
        return WorkItem.model_validate_json(path.read_text())

    def list(self) -> list[WorkItem]:
        if not self._dir.is_dir():
            return []
        items = [WorkItem.model_validate_json(p.read_text()) for p in self._dir.glob("*.json")]
        return sorted(items, key=lambda w: w.updated_at, reverse=True)

    def create(self, title: str, kind: Kind, workspace: str, now: datetime) -> WorkItem:
        item = WorkItem(id=_new_id(now), title=title, workspace=workspace, kind=kind,
                        created_at=now, updated_at=now)
        self.save(item)
        return item

    def _mutate(self, item_id: str, now: datetime | None) -> WorkItem:
        item = self.get(item_id)
        if item is None:
            raise KeyError(item_id)
        if now is not None:
            item.updated_at = now
        return item

    def link_spec(self, item_id: str, spec_id: str, now: datetime | None = None) -> None:
        item = self._mutate(item_id, now)
        item.spec_id = spec_id
        self.save(item)

    def add_task(self, item_id: str, task_slug: str, now: datetime | None = None,
                state: StateManager | None = None) -> None:
        item = self.get(item_id)
        if item is None:
            raise KeyError(item_id)
        if task_slug in item.task_slugs:
            return
        item.task_slugs.append(task_slug)
        if now is not None:
            item.updated_at = now
        self.save(item)
        if state is not None:
            # Reverse link: task.work_item_id, mirroring workitem_migrate.wrap_existing's
            # pass-2 mutation (workitem_migrate.py:46-49).
            def _set(s, _slug=task_slug, _wid=item_id):
                if _slug in s.tasks:
                    s.tasks[_slug].work_item_id = _wid
            state.mutate(_set)

    def add_thread(self, item_id: str, thread_id: str, now: datetime | None = None) -> None:
        item = self.get(item_id)
        if item is None:
            raise KeyError(item_id)
        if thread_id in item.thread_ids:
            return
        item.thread_ids.append(thread_id)
        if now is not None:
            item.updated_at = now
        self.save(item)

    def add_external_link(self, item_id: str, link: ExternalLink, now: datetime | None = None) -> None:
        item = self._mutate(item_id, now)
        item.external_links.append(link)
        self.save(item)

    def set_phase_override(self, item_id: str, phase: Phase, now: datetime | None = None) -> None:
        item = self._mutate(item_id, now)
        item.phase_override = phase
        self.save(item)

    def set_unattended(self, item_id: str, on: bool, now: datetime | None = None) -> None:
        item = self._mutate(item_id, now)
        item.unattended = on
        self.save(item)
