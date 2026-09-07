from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mship.core.daemon.status import daemon_is_running
from mship.core.persistence.backend import (
    StorageBackend,
    detect_backend,
    list_legacy_workitems,
    load_legacy_state,
)
from mship.core.persistence.database import WorkspaceDatabase
from mship.core.persistence.schema import storage_metadata
from mship.core.persistence.serialization import encode_datetime
from mship.core.persistence.task_repository import TaskRepository
from mship.core.persistence.workitem_repository import WorkItemRepository
from mship.core.state import StateManager, WorkspaceState
from mship.core.workitem import WorkItem


class MigrationPreflightError(RuntimeError):
    """The workspace cannot be migrated safely in its current state."""


class MigrationVerificationError(RuntimeError):
    """The candidate database did not reproduce the legacy state exactly."""


@dataclass(frozen=True)
class MigrationReport:
    migrated: bool
    database_path: Path
    backup_path: Path | None
    revision: str
    tasks: int
    work_items: int


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _call_stage(
    stage_hook: Callable[[str], None] | None,
    stage: str,
) -> None:
    if stage_hook is not None:
        stage_hook(stage)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _unique_path(parent: Path, stem: str) -> Path:
    candidate = parent / stem
    if not candidate.exists():
        return candidate
    return parent / f"{stem}-{uuid.uuid4().hex[:8]}"


def _legacy_fingerprints(state_dir: Path) -> tuple[str, str]:
    state_path = state_dir / "state.yaml"
    state_bytes = state_path.read_bytes() if state_path.is_file() else b""
    state_digest = hashlib.sha256(state_bytes).hexdigest()

    items_digest = hashlib.sha256()
    workitems_dir = state_dir / "workitems"
    if workitems_dir.is_dir():
        for path in sorted(workitems_dir.glob("*.json")):
            items_digest.update(path.name.encode())
            items_digest.update(b"\0")
            items_digest.update(hashlib.sha256(path.read_bytes()).digest())
    return state_digest, items_digest.hexdigest()


def _backup_legacy(state_dir: Path, now: datetime) -> Path:
    backup = _unique_path(
        state_dir / "backups",
        _timestamp(now),
    )
    backup.mkdir(parents=True)
    state_path = state_dir / "state.yaml"
    if state_path.is_file():
        shutil.copy2(state_path, backup / "state.yaml")
    workitems_dir = state_dir / "workitems"
    if workitems_dir.is_dir():
        backup_items = backup / "workitems"
        backup_items.mkdir()
        for path in sorted(workitems_dir.glob("*.json")):
            shutil.copy2(path, backup_items / path.name)
    return backup


def _verify_candidate(
    database: WorkspaceDatabase,
    expected_state: WorkspaceState,
    expected_items: list[WorkItem],
) -> None:
    tasks_repo = TaskRepository()
    items_repo = WorkItemRepository()
    with database.read() as connection:
        actual_state = WorkspaceState(tasks=tasks_repo.list(connection))
        actual_items = items_repo.list(connection, include_archived=True)
        foreign_key_errors = connection.exec_driver_sql(
            "PRAGMA foreign_key_check"
        ).all()
    if actual_state != expected_state:
        raise MigrationVerificationError(
            "candidate Task state does not match legacy state"
        )
    if {item.id: item for item in actual_items} != {
        item.id: item for item in expected_items
    }:
        raise MigrationVerificationError(
            "candidate WorkItem state does not match legacy state"
        )
    if foreign_key_errors:
        raise MigrationVerificationError(
            f"candidate database has foreign-key errors: {foreign_key_errors}"
        )


def _write_migration_metadata(
    database: WorkspaceDatabase,
    *,
    migrated_at: datetime,
    state_fingerprint: str,
    workitems_fingerprint: str,
) -> None:
    stamp = encode_datetime(migrated_at)
    values = {
        "migrated_at": stamp,
        "legacy_state_sha256": state_fingerprint,
        "legacy_workitems_sha256": workitems_fingerprint,
    }
    with database.write() as connection:
        connection.execute(
            storage_metadata.insert(),
            [
                {"key": key, "value": value, "updated_at": stamp}
                for key, value in values.items()
            ],
        )


def _existing_report(database: WorkspaceDatabase) -> MigrationReport:
    database.initialize()
    with database.read() as connection:
        task_count = len(TaskRepository().list(connection))
        item_count = len(WorkItemRepository().list(connection, include_archived=True))
    return MigrationReport(
        migrated=False,
        database_path=database.path,
        backup_path=None,
        revision=database.head_revision(),
        tasks=task_count,
        work_items=item_count,
    )


def migrate_legacy_state(
    state_dir: Path,
    *,
    daemon_probe: Callable[[], object | None] | None = None,
    now: datetime | None = None,
    stage_hook: Callable[[str], None] | None = None,
) -> MigrationReport:
    """Validate, back up, import, verify, and atomically activate legacy state."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    final_database = WorkspaceDatabase(state_dir)
    migration_time = now or datetime.now(timezone.utc)
    probe = daemon_probe or daemon_is_running

    with _exclusive_lock(state_dir / "state-migration.lock"):
        if final_database.path.is_file():
            return _existing_report(final_database)
        if probe():
            raise MigrationPreflightError(
                "the Mothership daemon is running; stop it before migration"
            )
        backend = detect_backend(state_dir)
        if backend is StorageBackend.EMPTY:
            return _existing_report(final_database)
        if backend is not StorageBackend.LEGACY:
            raise MigrationPreflightError(
                f"cannot migrate storage backend {backend.value!r}"
            )

        workitems_dir = state_dir / "workitems"
        item_paths = (
            sorted(workitems_dir.glob("*.json")) if workitems_dir.is_dir() else []
        )
        with ExitStack() as locks:
            locks.enter_context(_exclusive_lock(state_dir / "state.lock"))
            locks.enter_context(_exclusive_lock(workitems_dir / ".thread-link.lock"))
            for path in item_paths:
                locks.enter_context(
                    _exclusive_lock(path.with_name(path.name + ".lock"))
                )

            _call_stage(stage_hook, "validate")
            legacy_state = load_legacy_state(state_dir)
            legacy_items = list_legacy_workitems(
                workitems_dir,
                include_archived=True,
            )[0]
            state_fingerprint, items_fingerprint = _legacy_fingerprints(state_dir)

            _call_stage(stage_hook, "backup")
            backup_path = _backup_legacy(state_dir, migration_time)

            candidate_path = state_dir / (
                f".mothership.db.migrating-{uuid.uuid4().hex}"
            )
            candidate = WorkspaceDatabase(
                state_dir,
                database_path=candidate_path,
            )
            activated = False
            retired: list[tuple[Path, Path]] = []
            try:
                _call_stage(stage_hook, "alembic")
                candidate.initialize()

                _call_stage(stage_hook, "import")
                tasks_repo = TaskRepository()
                items_repo = WorkItemRepository()
                with candidate.write(immediate=True) as connection:
                    for slug in StateManager._dependency_order(legacy_state.tasks):
                        tasks_repo.insert(
                            connection,
                            legacy_state.tasks[slug],
                        )
                    for item in sorted(legacy_items, key=lambda value: value.id):
                        items_repo.insert(connection, item)

                _call_stage(stage_hook, "verify")
                _verify_candidate(candidate, legacy_state, legacy_items)
                _write_migration_metadata(
                    candidate,
                    migrated_at=migration_time,
                    state_fingerprint=state_fingerprint,
                    workitems_fingerprint=items_fingerprint,
                )
                revision = candidate.head_revision()
                candidate.checkpoint()
                candidate.dispose()

                _call_stage(stage_hook, "activate")
                os.replace(candidate_path, final_database.path)
                activated = True
                suffix = f".migrated-{_timestamp(migration_time)}"
                for source in (state_dir / "state.yaml", workitems_dir):
                    if not source.exists():
                        continue
                    destination = _unique_path(
                        source.parent,
                        source.name + suffix,
                    )
                    source.rename(destination)
                    retired.append((source, destination))
            except BaseException:
                for source, destination in reversed(retired):
                    if destination.exists():
                        destination.rename(source)
                if activated and final_database.path.exists():
                    os.replace(final_database.path, candidate_path)
                    activated = False
                raise
            finally:
                candidate.dispose()
                for path in (
                    candidate_path,
                    Path(str(candidate_path) + "-wal"),
                    Path(str(candidate_path) + "-shm"),
                ):
                    path.unlink(missing_ok=True)

    return MigrationReport(
        migrated=True,
        database_path=final_database.path,
        backup_path=backup_path,
        revision=revision,
        tasks=len(legacy_state.tasks),
        work_items=len(legacy_items),
    )
