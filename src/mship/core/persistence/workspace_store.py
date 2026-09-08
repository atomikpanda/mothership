from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection

from mship.core.persistence.backend import (
    StorageBackend,
    detect_backend,
    load_legacy_state,
)
from mship.core.persistence.database import WorkspaceDatabase
from mship.core.persistence.errors import LegacyMigrationRequired
from mship.core.persistence.task_repository import TaskRepository
from mship.core.persistence.workitem_repository import WorkItemRepository
from mship.core.state import WorkspaceState


@dataclass(frozen=True)
class WorkspaceTransaction:
    connection: Connection
    tasks: TaskRepository
    workitems: WorkItemRepository


class WorkspaceStore:
    """Select storage and provide one transaction boundary for domain repositories."""

    def __init__(
        self,
        state_dir: Path,
        database: WorkspaceDatabase | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.database = database or WorkspaceDatabase(self.state_dir)
        self.tasks = TaskRepository()
        self.workitems = WorkItemRepository()

    @property
    def backend(self) -> StorageBackend:
        return detect_backend(self.state_dir)

    def load_state(self) -> WorkspaceState:
        backend = self.backend
        if backend is StorageBackend.EMPTY:
            return WorkspaceState()
        if backend is StorageBackend.LEGACY:
            return load_legacy_state(self.state_dir)
        self.database.initialize()
        with self.database.read() as connection:
            return WorkspaceState(tasks=self.tasks.list(connection))

    def initialize_if_empty(self) -> bool:
        if self.backend is not StorageBackend.EMPTY:
            return False
        self.database.initialize()
        return True

    @contextmanager
    def read(self) -> Iterator[WorkspaceTransaction]:
        if self.backend is not StorageBackend.SQLITE:
            raise RuntimeError("SQLite workspace transaction requested without a database")
        self.database.initialize()
        with self.database.read() as connection:
            yield WorkspaceTransaction(connection, self.tasks, self.workitems)

    @contextmanager
    def write(self, *, immediate: bool = False) -> Iterator[WorkspaceTransaction]:
        backend = self.backend
        if backend is StorageBackend.LEGACY:
            raise LegacyMigrationRequired(self.state_dir)
        self.database.initialize()
        with self.database.write(immediate=immediate) as connection:
            yield WorkspaceTransaction(connection, self.tasks, self.workitems)
