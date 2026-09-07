from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from sqlalchemy import Connection

    from mship.core.persistence.workspace_store import WorkspaceStore


class TestResult(BaseModel):
    status: Literal["pass", "fail", "skip"]
    at: datetime


class DependencyEdge(BaseModel):
    upstream_slug: str
    created_at: datetime


class Task(BaseModel):
    slug: str
    description: str
    phase: Literal["plan", "dev", "review", "run"]
    created_at: datetime
    affected_repos: list[str]
    worktrees: dict[str, Path] = {}
    branch: str
    test_results: dict[str, TestResult] = {}
    blocked_reason: str | None = None
    blocked_at: datetime | None = None
    pr_urls: dict[str, str] = {}
    finished_at: datetime | None = None
    phase_entered_at: datetime | None = None
    last_activity_at: datetime | None = None
    active_repo: str | None = None
    last_switched_at_sha: dict[str, dict[str, str]] = {}
    test_iteration: int = 0
    base_branch: str | None = None
    # Non-default base pinned at spawn time via `--base` (stacked PRs, #42).
    # None for ordinary tasks; when set, `finish` targets it as the PR base and
    # base-relative checks (close/context) compare against it. Kept distinct from
    # `base_branch` (which records the effective base — the stacked branch when
    # `--base` is given, else the workspace default): resolve_base only consults
    # base_override, so a plain spawn (None here) never overrides a repo's
    # configured base.
    base_override: str | None = None
    passive_repos: set[str] = set()
    spec_id: str | None = None
    depends_on: list[DependencyEdge] = []
    work_item_id: str | None = None


class WorkspaceState(BaseModel):
    # extra="ignore" lets legacy state.yaml with `current_task:` load cleanly
    # (the field is silently dropped during the multi-task migration).
    model_config = ConfigDict(extra="ignore")
    tasks: dict[str, Task] = {}


class StateManager:
    """Compatibility facade over legacy state.yaml and transactional SQLite."""

    def __init__(
        self,
        state_dir: Path | None = None,
        *,
        workspace_store: WorkspaceStore | None = None,
    ) -> None:
        if workspace_store is None:
            if state_dir is None:
                raise TypeError("state_dir is required when workspace_store is omitted")
            from mship.core.persistence.workspace_store import WorkspaceStore

            workspace_store = WorkspaceStore(state_dir)
        self._store = workspace_store

    @property
    def state_dir(self) -> Path:
        """Canonical state directory shared by this workspace's stores."""
        return self._store.state_dir

    @property
    def workspace_store(self) -> WorkspaceStore:
        """Shared transaction boundary for cross-entity lifecycle operations."""
        return self._store

    def load(self) -> WorkspaceState:
        return self._store.load_state()

    def save(self, state: WorkspaceState) -> None:
        with self._store.write(immediate=True) as transaction:
            self._persist_state(transaction, state)

    def mutate(self, fn: Callable[[WorkspaceState], None]) -> WorkspaceState:
        """Read-modify-write in one immediate transaction. No lost updates."""
        with self._store.write(immediate=True) as transaction:
            state = WorkspaceState(tasks=transaction.tasks.list(transaction.connection))
            fn(state)
            self._persist_state(transaction, state)
            return state

    def insert_task(
        self,
        task: Task,
        *,
        connection: Connection | None = None,
    ) -> None:
        """Insert one Task, optionally joining an existing workspace transaction."""
        if connection is not None:
            self._store.tasks.insert(connection, task)
            return
        with self._store.write(immediate=True) as transaction:
            transaction.tasks.insert(transaction.connection, task)

    def mutate_task(
        self,
        slug: str,
        fn: Callable[[Task], None],
        *,
        connection: Connection | None = None,
    ) -> Task:
        """Mutate one named Task without rewriting any other Task row."""
        if connection is not None:
            return self._mutate_task(connection, slug, fn)
        with self._store.write(immediate=True) as transaction:
            return self._mutate_task(transaction.connection, slug, fn)

    def _mutate_task(
        self,
        connection: Connection,
        slug: str,
        fn: Callable[[Task], None],
    ) -> Task:
        task = self._store.tasks.get(connection, slug)
        if task is None:
            raise KeyError(slug)
        before = task.model_copy(deep=True)
        fn(task)
        if task.slug != slug:
            raise ValueError(
                f"task mutation changed slug from {slug!r} to {task.slug!r}"
            )
        if task != before:
            self._store.tasks.replace(connection, task)
        return task

    def mutate_tasks(
        self,
        slugs: Iterable[str],
        fn: Callable[[dict[str, Task]], None],
        *,
        connection: Connection | None = None,
    ) -> dict[str, Task]:
        """Atomically mutate only the requested Task rows."""
        requested = sorted(set(slugs))
        if connection is not None:
            return self._mutate_tasks(connection, requested, fn)
        with self._store.write(immediate=True) as transaction:
            return self._mutate_tasks(transaction.connection, requested, fn)

    def _mutate_tasks(
        self,
        connection: Connection,
        slugs: list[str],
        fn: Callable[[dict[str, Task]], None],
    ) -> dict[str, Task]:
        selected: dict[str, Task] = {}
        before: dict[str, Task] = {}
        for slug in slugs:
            task = self._store.tasks.get(connection, slug)
            if task is None:
                raise KeyError(slug)
            selected[slug] = task
            before[slug] = task.model_copy(deep=True)

        fn(selected)
        unexpected = set(selected) - set(slugs)
        if unexpected:
            raise ValueError(
                "task mutation added unrequested slug(s): "
                + ", ".join(sorted(unexpected))
            )
        for slug, task in selected.items():
            if task.slug != slug:
                raise ValueError(
                    f"task mutation changed slug from {slug!r} to {task.slug!r}"
                )

        removed = set(slugs) - set(selected)
        dependencies_changed = bool(removed) or any(
            selected[slug].depends_on != before[slug].depends_on
            for slug in selected
        )
        if dependencies_changed:
            all_tasks = self._store.tasks.list(connection)
            for slug in removed:
                all_tasks.pop(slug, None)
            all_tasks.update(selected)
            self._dependency_order(all_tasks)

        for slug in sorted(removed):
            self._store.tasks.delete(connection, slug)
        for slug in sorted(selected):
            if selected[slug] != before[slug]:
                self._store.tasks.replace(connection, selected[slug])
        return selected

    def delete_task(
        self,
        slug: str,
        *,
        connection: Connection | None = None,
    ) -> bool:
        """Delete one Task, optionally joining an existing workspace transaction."""
        if connection is not None:
            return self._store.tasks.delete(connection, slug)
        with self._store.write(immediate=True) as transaction:
            return transaction.tasks.delete(transaction.connection, slug)

    @staticmethod
    def _persist_state(transaction, state: WorkspaceState) -> None:
        existing = transaction.tasks.list(transaction.connection)
        for slug in existing.keys() - state.tasks.keys():
            transaction.tasks.delete(transaction.connection, slug)
        for slug in StateManager._dependency_order(state.tasks):
            task = state.tasks[slug]
            previous = existing.get(slug)
            if previous is None:
                transaction.tasks.insert(transaction.connection, task)
            elif previous != task:
                transaction.tasks.replace(transaction.connection, task)

    @staticmethod
    def _dependency_order(tasks: dict[str, Task]) -> list[str]:
        for key, task in tasks.items():
            if key != task.slug:
                raise ValueError(
                    f"task mapping key {key!r} does not match slug {task.slug!r}"
                )

        pending = set(tasks)
        ordered: list[str] = []
        while pending:
            ready = sorted(
                slug
                for slug in pending
                if all(
                    edge.upstream_slug not in pending
                    for edge in tasks[slug].depends_on
                )
            )
            if not ready:
                raise ValueError(
                    "task dependency cycle: " + ", ".join(sorted(pending))
                )
            ordered.extend(ready)
            pending.difference_update(ready)
        return ordered

    def record_activity(self, slug: str, now: datetime | None = None) -> None:
        """Stamp `last_activity_at` on a task — the agent-agnostic activity heartbeat.

        Cheap: one field write under the same exclusive lock as any other
        mutation. A no-op when `slug` is unknown, so callers that may pass a
        slug that isn't (yet) a task (e.g. `mship spec apply` before dispatch)
        stay safe.
        """
        stamp = now or datetime.now(timezone.utc)

        try:
            self.mutate_task(
                slug,
                lambda task: setattr(task, "last_activity_at", stamp),
            )
        except KeyError:
            pass
