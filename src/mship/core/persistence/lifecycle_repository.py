from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime

from sqlalchemy import Connection

from mship.core.persistence.workspace_store import WorkspaceStore
from mship.core.persistence.serialization import PersistenceDecodeError
from mship.core.state import Task
from mship.core.workitem import WorkItem
from mship.core.workitem_lifecycle import TaskMetadataRetentionConflictError
from mship.core.workitem_store import TaskLinkAmbiguousError


class LifecycleRepository:
    """Atomic Task↔WorkItem ownership and delivery-metadata operations."""

    def __init__(self, store: WorkspaceStore) -> None:
        self._store = store

    def _checkpoint(self, _stage: str) -> None:
        """Private fault-injection seam used by transaction rollback tests."""

    def _owner_ids(self, connection: Connection, task_slug: str) -> list[str]:
        return sorted(
            item.id
            for item in self._store.workitems.list(
                connection,
                include_archived=True,
            )
            if task_slug in item.task_slugs
        )

    def _require_owner_available(
        self,
        connection: Connection,
        task_slug: str,
        work_item_id: str,
    ) -> WorkItem:
        item = self._store.workitems.get(connection, work_item_id)
        if item is None:
            raise KeyError(work_item_id)
        owners = self._owner_ids(connection, task_slug)
        conflicting = [owner for owner in owners if owner != work_item_id]
        if conflicting:
            raise TaskLinkAmbiguousError(
                task_slug,
                sorted([*conflicting, work_item_id]),
            )
        return item

    def _link_loaded(
        self,
        connection: Connection,
        task: Task,
        item: WorkItem,
        *,
        now: datetime,
        spec_id: str | None = None,
    ) -> None:
        task_changed = task.work_item_id != item.id
        task.work_item_id = item.id
        if spec_id is not None and task.spec_id != spec_id:
            task.spec_id = spec_id
            task_changed = True

        item_changed = False
        if task.slug not in item.task_slugs:
            item.task_slugs.append(task.slug)
            item_changed = True
        if spec_id is not None and item.spec_id != spec_id:
            item.spec_id = spec_id
            item_changed = True

        if task_changed:
            self._store.tasks.replace(connection, task)
            self._checkpoint("after_task_link")
        if item_changed:
            item.updated_at = now
            self._store.workitems.replace(connection, item)

    def register_task(
        self,
        task: Task,
        work_item_id: str,
        *,
        now: datetime,
    ) -> None:
        """Insert a Task and establish its durable WorkItem owner atomically."""
        registered = task.model_copy(deep=True)
        registered.work_item_id = work_item_id
        with self._store.write(immediate=True) as transaction:
            if transaction.tasks.get(transaction.connection, registered.slug):
                raise KeyError(registered.slug)
            transaction.tasks.insert(transaction.connection, registered)
            self._checkpoint("after_task_insert")
            item = self._require_owner_available(
                transaction.connection,
                registered.slug,
                work_item_id,
            )
            self._link_loaded(
                transaction.connection,
                registered,
                item,
                now=now,
            )

    def link_task(
        self,
        work_item_id: str,
        task_slug: str,
        *,
        now: datetime,
    ) -> None:
        """Set both sides of an existing Task↔WorkItem link atomically."""
        with self._store.write(immediate=True) as transaction:
            task = transaction.tasks.get(transaction.connection, task_slug)
            if task is None:
                raise KeyError(task_slug)
            item = self._require_owner_available(
                transaction.connection,
                task_slug,
                work_item_id,
            )
            self._link_loaded(
                transaction.connection,
                task,
                item,
                now=now,
            )

    def bind_spec(
        self,
        task_slug: str,
        work_item_id: str,
        spec_id: str,
        *,
        now: datetime,
    ) -> None:
        """Bind Task, WorkItem ownership, and their shared spec id atomically."""
        with self._store.write(immediate=True) as transaction:
            task = transaction.tasks.get(transaction.connection, task_slug)
            if task is None:
                raise KeyError(task_slug)
            item = self._require_owner_available(
                transaction.connection,
                task_slug,
                work_item_id,
            )
            self._link_loaded(
                transaction.connection,
                task,
                item,
                now=now,
                spec_id=spec_id,
            )

    @staticmethod
    def _validate_expected_task(
        task: Task,
        expected_task: Task | None,
    ) -> None:
        if expected_task is not None and task != expected_task:
            raise TaskMetadataRetentionConflictError(
                task.slug,
                "its Task revision changed after external checks",
            )

    def _resolve_owner_item(
        self,
        connection: Connection,
        task: Task,
        *,
        allow_missing_workitem: bool = False,
    ) -> WorkItem | None:
        owners = self._owner_ids(connection, task.slug)
        if len(owners) > 1:
            raise TaskMetadataRetentionConflictError(
                task.slug,
                f"forward WorkItem link is ambiguous ({', '.join(owners)})",
            )
        item_id = owners[0] if owners else task.work_item_id
        if item_id is None:
            return None
        item = self._store.workitems.get(connection, item_id)
        if item is None:
            if allow_missing_workitem:
                return None
            raise TaskMetadataRetentionConflictError(
                task.slug,
                f"linked work item {item_id!r} is unavailable",
            )
        return item

    def _retain_loaded(
        self,
        connection: Connection,
        task: Task,
        *,
        now: datetime,
        allow_missing_workitem: bool = False,
    ) -> None:
        item = self._resolve_owner_item(
            connection,
            task,
            allow_missing_workitem=allow_missing_workitem,
        )
        if item is None:
            return
        repos = list(dict.fromkeys([*item.affected_repos, *task.affected_repos]))
        urls = list(dict.fromkeys([*item.pr_urls, *task.pr_urls.values()]))
        if repos == item.affected_repos and urls == item.pr_urls:
            return
        item.affected_repos = repos
        item.pr_urls = urls
        item.updated_at = now
        self._store.workitems.replace(connection, item)

    def retain_and_delete_task(
        self,
        task_slug: str,
        *,
        now: datetime,
        expected_task: Task | None = None,
    ) -> bool:
        """Retain delivery metadata and delete transient Task state atomically."""
        with self._store.write(immediate=True) as transaction:
            task = transaction.tasks.get(transaction.connection, task_slug)
            if task is None:
                if expected_task is not None:
                    raise TaskMetadataRetentionConflictError(
                        task_slug,
                        "its Task revision changed after external checks",
                    )
                return False
            self._validate_expected_task(task, expected_task)
            self._retain_loaded(transaction.connection, task, now=now)
            self._checkpoint("before_task_delete")
            return transaction.tasks.delete(transaction.connection, task_slug)

    def retain_and_delete_tasks(
        self,
        expected_tasks: Mapping[str, Task],
        *,
        now: datetime,
    ) -> set[str]:
        """Atomically retain and delete a snapshot of several Tasks."""
        with self._store.write(immediate=True) as transaction:
            loaded: dict[str, Task] = {}
            for task_slug, expected_task in expected_tasks.items():
                task = transaction.tasks.get(transaction.connection, task_slug)
                if task is None:
                    raise TaskMetadataRetentionConflictError(
                        task_slug,
                        "its Task revision changed after external checks",
                    )
                self._validate_expected_task(task, expected_task)
                loaded[task_slug] = task

            for task in loaded.values():
                self._retain_loaded(transaction.connection, task, now=now)
            self._checkpoint("before_tasks_delete")
            for task_slug in loaded:
                transaction.tasks.delete(transaction.connection, task_slug)
            return set(loaded)

    def retain_and_prune_task_repos(
        self,
        task_slug: str,
        repos: Collection[str],
        *,
        now: datetime,
        expected_task: Task | None = None,
    ) -> bool:
        """Prune worktree mappings; retain and delete if the final mapping leaves."""
        with self._store.write(immediate=True) as transaction:
            task = transaction.tasks.get(transaction.connection, task_slug)
            if task is None:
                if expected_task is not None:
                    raise TaskMetadataRetentionConflictError(
                        task_slug,
                        "its Task revision changed after external checks",
                    )
                return False
            self._validate_expected_task(task, expected_task)
            for repo in set(repos):
                task.worktrees.pop(repo, None)
            if task.worktrees:
                transaction.tasks.replace(transaction.connection, task)
                return False
            self._retain_loaded(transaction.connection, task, now=now)
            self._checkpoint("before_task_delete")
            transaction.tasks.delete(transaction.connection, task_slug)
            return True

    def record_pr_urls(
        self,
        task_slug: str,
        pr_urls: Mapping[str, str],
        *,
        now: datetime,
        allow_unreadable_workitem: bool = False,
    ) -> Task:
        """Record Task PRs and mirror retained WorkItem URLs in one short write.

        A hotfix finish may explicitly preserve the Task result even when a
        linked WorkItem is unreadable or has gone stale/missing. Normal calls remain atomic:
        any WorkItem read or update failure rolls the Task write back.
        """
        with self._store.write(immediate=True) as transaction:
            task = transaction.tasks.get(transaction.connection, task_slug)
            if task is None:
                raise KeyError(task_slug)
            task.pr_urls.update(pr_urls)
            transaction.tasks.replace(transaction.connection, task)
            self._checkpoint("after_task_replace")
            try:
                self._retain_loaded(
                    transaction.connection,
                    task,
                    now=now,
                    allow_missing_workitem=allow_unreadable_workitem,
                )
            except PersistenceDecodeError:
                if not allow_unreadable_workitem:
                    raise
            return task
