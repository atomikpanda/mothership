from __future__ import annotations

import fcntl
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from mship.core.state import StateManager
from mship.core.workitem import ExternalLink, Kind, Phase, WorkItem

__all__ = ["WorkItemStore", "ThreadAlreadyLinkedError"]


def _new_id(now: datetime) -> str:
    return f"wi-{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"


@contextmanager
def _locked(lock_path: Path, mode: int):
    """Advisory flock on `lock_path` (mirrors state.py's `_locked`).

    mode: fcntl.LOCK_SH (shared read) or fcntl.LOCK_EX (exclusive write).
    Released when the context exits. A per-item lock file lets writers to
    DIFFERENT items proceed in parallel; only same-item writers serialize.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as lf:
        fcntl.flock(lf, mode)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


class ThreadAlreadyLinkedError(Exception):
    """Raised when linking a thread to a WorkItem that another WorkItem already owns.

    Threads are single-owner: the thread->WorkItem resolver (view/thread_links) assumes a thread
    appears in at most one item's `thread_ids`, so the write side refuses dual membership rather than
    silently creating an ambiguous link.
    """

    def __init__(self, thread_id: str, owner_id: str) -> None:
        self.thread_id = thread_id
        self.owner_id = owner_id
        super().__init__(f"thread {thread_id!r} is already linked to work item {owner_id!r}")


class WorkItemStore:
    """Filesystem registry for work items: one JSON file per item."""

    def __init__(self, workitems_dir: Path) -> None:
        self._dir = Path(workitems_dir)

    def _path(self, item_id: str) -> Path:
        if (not item_id or "/" in item_id or "\\" in item_id
                or item_id in (".", "..") or item_id.startswith(".")):
            raise ValueError(f"unsafe work item id: {item_id!r}")
        return self._dir / f"{item_id}.json"

    def _lock_path(self, item_id: str) -> Path:
        """Per-item lock file (`<id>.json.lock`). Reuses `_path`'s id validation.
        Not matched by `list()`'s `*.json` glob, so it stays invisible to reads."""
        p = self._path(item_id)
        return p.with_name(p.name + ".lock")

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

    def list(self, include_archived: bool = False) -> list[WorkItem]:
        if not self._dir.is_dir():
            return []
        items = [WorkItem.model_validate_json(p.read_text()) for p in self._dir.glob("*.json")]
        if not include_archived:
            items = [item for item in items if not item.archived]
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
        with _locked(self._lock_path(item_id), fcntl.LOCK_EX):
            item = self._mutate(item_id, now)
            item.spec_id = spec_id
            self.save(item)

    def link_plan(self, item_id: str, plan_path: str, now: datetime | None = None) -> None:
        with _locked(self._lock_path(item_id), fcntl.LOCK_EX):
            item = self._mutate(item_id, now)
            item.plan_path = plan_path
            self.save(item)

    def add_task(self, item_id: str, task_slug: str, now: datetime | None = None,
                state: StateManager | None = None) -> None:
        # Exclusive lock spans get+append+save so concurrent add_task calls to the
        # same item can't clobber each other's task_slugs (MOS-233).
        with _locked(self._lock_path(item_id), fcntl.LOCK_EX):
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
            # pass-2 mutation (workitem_migrate.py:46-49). StateManager.mutate takes its
            # own state.lock, so keep it outside this item lock to avoid lock coupling.
            def _set(s, _slug=task_slug, _wid=item_id):
                if _slug in s.tasks:
                    s.tasks[_slug].work_item_id = _wid
            state.mutate(_set)

    def _thread_owner(self, thread_id: str, exclude: str) -> str | None:
        """Id of a WorkItem (other than `exclude`) whose thread_ids contains `thread_id`, else None.
        Scans archived items too — an archived item still holds its threads in stored data, so it
        still counts as the thread's owner for the single-owner invariant."""
        for w in self.list(include_archived=True):
            if w.id != exclude and thread_id in w.thread_ids:
                return w.id
        return None

    def _thread_link_lock_path(self) -> Path:
        """Store-wide lock serializing all `add_thread` calls. The per-item locks let writers to
        DIFFERENT items run in parallel, so the cross-item ownership scan + append would otherwise
        race (two concurrent adds of the same thread to different items could both see "no owner"
        and both save → dual membership). Held around the scan+append so the exclusivity check is
        atomic. Starts with '.', so `list()`'s `*.json` glob never reads it."""
        return self._dir / ".thread-link.lock"

    def add_thread(self, item_id: str, thread_id: str, now: datetime | None = None) -> None:
        # Outer store-wide lock makes the ownership scan + append atomic ACROSS items; inner per-item
        # lock keeps this item's read-modify-write atomic against other same-item writers. Ordering is
        # always store-lock → item-lock (other methods take only the item lock), so no deadlock cycle.
        with _locked(self._thread_link_lock_path(), fcntl.LOCK_EX):
            with _locked(self._lock_path(item_id), fcntl.LOCK_EX):
                item = self.get(item_id)
                if item is None:
                    raise KeyError(item_id)
                if thread_id in item.thread_ids:
                    return  # already ours — idempotent no-op
                # Exclusive membership: a thread belongs to at most one WorkItem (the resolver assumes
                # a single owner). If another item holds it, refuse rather than create dual membership.
                owner = self._thread_owner(thread_id, exclude=item_id)
                if owner is not None:
                    raise ThreadAlreadyLinkedError(thread_id, owner)
                item.thread_ids.append(thread_id)
                if now is not None:
                    item.updated_at = now
                self.save(item)

    def add_external_link(self, item_id: str, link: ExternalLink, now: datetime | None = None) -> None:
        with _locked(self._lock_path(item_id), fcntl.LOCK_EX):
            item = self._mutate(item_id, now)
            item.external_links.append(link)
            self.save(item)

    def set_phase_override(self, item_id: str, phase: Phase | None, now: datetime | None = None) -> None:
        """Set the manual phase override, or clear it (return to derived phase) when
        `phase` is None. Raises KeyError if the item does not exist."""
        with _locked(self._lock_path(item_id), fcntl.LOCK_EX):
            item = self._mutate(item_id, now)
            item.phase_override = phase
            self.save(item)

    def set_unattended(self, item_id: str, on: bool, now: datetime | None = None) -> None:
        with _locked(self._lock_path(item_id), fcntl.LOCK_EX):
            item = self._mutate(item_id, now)
            item.unattended = on
            self.save(item)

    def archive(self, item_id: str, now: datetime | None = None) -> None:
        """Soft-delete: mark the item archived so it's excluded from list() by
        default. Raises KeyError if the item does not exist."""
        with _locked(self._lock_path(item_id), fcntl.LOCK_EX):
            item = self._mutate(item_id, now)
            item.archived = True
            self.save(item)

    def unarchive(self, item_id: str, now: datetime | None = None) -> None:
        """Reverse of archive(): clear the archived flag. Raises KeyError if the
        item does not exist."""
        with _locked(self._lock_path(item_id), fcntl.LOCK_EX):
            item = self._mutate(item_id, now)
            item.archived = False
            self.save(item)
