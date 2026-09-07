from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from mship.core.persistence.backend import (
    StorageBackend,
    get_legacy_workitem,
    list_legacy_workitems,
)
from mship.core.persistence.workspace_store import WorkspaceStore
from mship.core.state import StateManager
from mship.core.workitem import ExternalLink, Kind, Phase, WorkItem

__all__ = ["TaskLinkAmbiguousError", "ThreadAlreadyLinkedError", "WorkItemStore"]


def _new_id(now: datetime) -> str:
    return f"wi-{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"


class TaskLinkAmbiguousError(RuntimeError):
    """Raised when a task's forward link belongs to multiple WorkItems."""

    def __init__(self, task_slug: str, item_ids: list[str]) -> None:
        self.task_slug = task_slug
        self.item_ids = item_ids
        super().__init__(
            f"task {task_slug!r} is linked to multiple work items: {', '.join(item_ids)}",
        )


class ThreadAlreadyLinkedError(Exception):
    """Raised when a thread already belongs to another WorkItem."""

    def __init__(self, thread_id: str, owner_id: str) -> None:
        self.thread_id = thread_id
        self.owner_id = owner_id
        super().__init__(
            f"thread {thread_id!r} is already linked to work item {owner_id!r}"
        )


class WorkItemStore:
    """Compatibility facade over legacy JSON and transactional SQLite."""

    def __init__(
        self,
        workitems_dir: Path,
        *,
        workspace_store: WorkspaceStore | None = None,
    ) -> None:
        self._dir = Path(workitems_dir)
        self._store = workspace_store or WorkspaceStore(self._dir.parent)

    def _path(self, item_id: str) -> Path:
        if (
            not item_id
            or "/" in item_id
            or "\\" in item_id
            or item_id in (".", "..")
            or item_id.startswith(".")
        ):
            raise ValueError(f"unsafe work item id: {item_id!r}")
        return self._dir / f"{item_id}.json"

    def save(self, item: WorkItem) -> Path:
        self._path(item.id)
        with self._store.write(immediate=True) as transaction:
            existing = transaction.workitems.get(transaction.connection, item.id)
            self._validate_ownership(
                item,
                transaction.workitems.list(
                    transaction.connection,
                    include_archived=True,
                ),
            )
            if existing is None:
                transaction.workitems.insert(transaction.connection, item)
            else:
                transaction.workitems.replace(transaction.connection, item)
        return self._store.database.path

    @staticmethod
    def _validate_ownership(
        item: WorkItem,
        candidates: list[WorkItem],
    ) -> None:
        others = [candidate for candidate in candidates if candidate.id != item.id]
        for task_slug in item.task_slugs:
            owner = next(
                (
                    candidate.id
                    for candidate in others
                    if task_slug in candidate.task_slugs
                ),
                None,
            )
            if owner is not None:
                raise TaskLinkAmbiguousError(
                    task_slug,
                    sorted([owner, item.id]),
                )
        for thread_id in item.thread_ids:
            owner = next(
                (
                    candidate.id
                    for candidate in others
                    if thread_id in candidate.thread_ids
                ),
                None,
            )
            if owner is not None:
                raise ThreadAlreadyLinkedError(thread_id, owner)

    def get(self, item_id: str) -> WorkItem | None:
        self._path(item_id)
        backend = self._store.backend
        if backend is StorageBackend.EMPTY:
            return None
        if backend is StorageBackend.LEGACY:
            return get_legacy_workitem(self._dir, item_id)
        with self._store.read() as transaction:
            return transaction.workitems.get(transaction.connection, item_id)

    def list(self, include_archived: bool = False) -> list[WorkItem]:
        backend = self._store.backend
        if backend is StorageBackend.EMPTY:
            return []
        if backend is StorageBackend.LEGACY:
            return list_legacy_workitems(
                self._dir,
                include_archived=include_archived,
            )[0]
        with self._store.read() as transaction:
            return transaction.workitems.list(
                transaction.connection,
                include_archived=include_archived,
            )

    def list_tolerant_with_uncertainty(
        self,
        include_archived: bool = False,
    ) -> tuple[list[WorkItem], bool]:
        backend = self._store.backend
        if backend is StorageBackend.EMPTY:
            return [], False
        if backend is StorageBackend.LEGACY:
            return list_legacy_workitems(
                self._dir,
                include_archived=include_archived,
                tolerant=True,
            )
        with self._store.read() as transaction:
            return transaction.workitems.list_tolerant_with_uncertainty(
                transaction.connection,
                include_archived=include_archived,
            )

    def list_tolerant(self, include_archived: bool = False) -> list[WorkItem]:
        return self.list_tolerant_with_uncertainty(include_archived)[0]

    def create(
        self,
        title: str,
        kind: Kind,
        workspace: str,
        now: datetime,
    ) -> WorkItem:
        item = WorkItem(
            id=_new_id(now),
            title=title,
            workspace=workspace,
            kind=kind,
            created_at=now,
            updated_at=now,
        )
        self.save(item)
        return item

    def _mutate_item(
        self,
        item_id: str,
        now: datetime | None,
        fn: Callable[[WorkItem], None],
    ) -> WorkItem:
        self._path(item_id)
        with self._store.write(immediate=True) as transaction:
            item = transaction.workitems.get(transaction.connection, item_id)
            if item is None:
                raise KeyError(item_id)
            fn(item)
            if now is not None:
                item.updated_at = now
            transaction.workitems.replace(transaction.connection, item)
            return item

    def link_spec(
        self,
        item_id: str,
        spec_id: str,
        now: datetime | None = None,
    ) -> None:
        self._mutate_item(
            item_id,
            now,
            lambda item: setattr(item, "spec_id", spec_id),
        )

    def link_plan(
        self,
        item_id: str,
        plan_path: str,
        now: datetime | None = None,
    ) -> None:
        self._mutate_item(
            item_id,
            now,
            lambda item: setattr(item, "plan_path", plan_path),
        )

    def add_task(
        self,
        item_id: str,
        task_slug: str,
        now: datetime | None = None,
        state: StateManager | None = None,
    ) -> None:
        self._path(item_id)
        with self._store.write(immediate=True) as transaction:
            item = transaction.workitems.get(transaction.connection, item_id)
            if item is None:
                raise KeyError(item_id)
            if task_slug in item.task_slugs:
                return
            owner_ids = [
                candidate.id
                for candidate in transaction.workitems.list(
                    transaction.connection,
                    include_archived=True,
                )
                if task_slug in candidate.task_slugs
            ]
            if owner_ids:
                raise TaskLinkAmbiguousError(
                    task_slug,
                    sorted([*owner_ids, item_id]),
                )
            item.task_slugs.append(task_slug)
            if now is not None:
                item.updated_at = now
            transaction.workitems.replace(transaction.connection, item)
        if state is not None:
            def _set(current, slug=task_slug, work_item_id=item_id):
                if slug in current.tasks:
                    current.tasks[slug].work_item_id = work_item_id

            state.mutate(_set)

    def resolve_task_workitem_id(
        self,
        task_slug: str,
        reverse_item_id: str | None,
    ) -> str | None:
        forward_ids = sorted(
            item.id
            for item in self.list(include_archived=True)
            if task_slug in item.task_slugs
        )
        if len(forward_ids) > 1:
            raise TaskLinkAmbiguousError(task_slug, forward_ids)
        return forward_ids[0] if forward_ids else reverse_item_id

    def retain_task_metadata(self, task, *, item_id: str | None = None) -> bool:
        item_id = item_id if item_id is not None else getattr(task, "work_item_id", None)
        if not item_id:
            return True
        self._path(item_id)
        repos = list(getattr(task, "affected_repos", []) or [])
        pr_urls = list((getattr(task, "pr_urls", {}) or {}).values())
        with self._store.write(immediate=True) as transaction:
            item = transaction.workitems.get(transaction.connection, item_id)
            if item is None:
                return False
            retained_repos = list(dict.fromkeys([*item.affected_repos, *repos]))
            retained_pr_urls = list(dict.fromkeys([*item.pr_urls, *pr_urls]))
            if (
                retained_repos == item.affected_repos
                and retained_pr_urls == item.pr_urls
            ):
                return True
            item.affected_repos = retained_repos
            item.pr_urls = retained_pr_urls
            item.updated_at = datetime.now(timezone.utc)
            transaction.workitems.replace(transaction.connection, item)
            return True

    def _thread_owner(self, thread_id: str, exclude: str) -> str | None:
        for item in self.list(include_archived=True):
            if item.id != exclude and thread_id in item.thread_ids:
                return item.id
        return None

    def add_thread(
        self,
        item_id: str,
        thread_id: str,
        now: datetime | None = None,
    ) -> None:
        self._path(item_id)
        with self._store.write(immediate=True) as transaction:
            item = transaction.workitems.get(transaction.connection, item_id)
            if item is None:
                raise KeyError(item_id)
            if thread_id in item.thread_ids:
                return
            owner = next(
                (
                    candidate.id
                    for candidate in transaction.workitems.list(
                        transaction.connection,
                        include_archived=True,
                    )
                    if candidate.id != item_id
                    and thread_id in candidate.thread_ids
                ),
                None,
            )
            if owner is not None:
                raise ThreadAlreadyLinkedError(thread_id, owner)
            item.thread_ids.append(thread_id)
            if now is not None:
                item.updated_at = now
            transaction.workitems.replace(transaction.connection, item)

    def add_external_link(
        self,
        item_id: str,
        link: ExternalLink,
        now: datetime | None = None,
    ) -> None:
        self._mutate_item(
            item_id,
            now,
            lambda item: item.external_links.append(link),
        )

    def set_phase_override(
        self,
        item_id: str,
        phase: Phase | None,
        now: datetime | None = None,
    ) -> None:
        self._mutate_item(
            item_id,
            now,
            lambda item: setattr(item, "phase_override", phase),
        )

    def set_unattended(
        self,
        item_id: str,
        on: bool,
        now: datetime | None = None,
    ) -> None:
        self._mutate_item(
            item_id,
            now,
            lambda item: setattr(item, "unattended", on),
        )

    def archive(self, item_id: str, now: datetime | None = None) -> None:
        self._mutate_item(
            item_id,
            now,
            lambda item: setattr(item, "archived", True),
        )

    def unarchive(self, item_id: str, now: datetime | None = None) -> None:
        self._mutate_item(
            item_id,
            now,
            lambda item: setattr(item, "archived", False),
        )
