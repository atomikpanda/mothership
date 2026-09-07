from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
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

        def _apply(state: WorkspaceState) -> None:
            task = state.tasks.get(slug)
            if task is not None:
                task.last_activity_at = stamp

        self.mutate(_apply)
