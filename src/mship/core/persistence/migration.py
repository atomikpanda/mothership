from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from mship.core.daemon.status import daemon_is_running
from mship.core.persistence.backend import (
    StorageBackend,
    _legacy_workitem_entries,
    detect_backend,
    list_legacy_workitems,
    load_legacy_state,
)
from mship.core.persistence.database import (
    DatabaseRevisionError,
    WorkspaceDatabase,
    make_alembic_config,
)
from mship.core.persistence.schema import storage_metadata
from mship.core.persistence.migration_ownership import (
    OwnershipConflict,
    OwnershipPlan,
    apply_ownership_plan,
    plan_ownership,
)
from mship.core.persistence.serialization import encode_datetime
from mship.core.persistence.task_repository import TaskRepository
from mship.core.persistence.workitem_repository import WorkItemRepository
from mship.core.state import StateManager, WorkspaceState
from mship.core.workitem import WorkItem


class MigrationPreflightError(RuntimeError):
    """The workspace cannot be migrated safely in its current state."""


class OwnershipConflictError(MigrationPreflightError):
    """Legacy WorkItem ownership must be resolved before migration."""

    def __init__(self, plan: OwnershipPlan) -> None:
        self.plan = plan
        details = "; ".join(
            f"{conflict.task_slug}: {', '.join(conflict.owner_ids)} ({conflict.reason})"
            for conflict in plan.conflicts
        )
        super().__init__(f"unresolved legacy WorkItem ownership conflicts: {details}")


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
    ownership: OwnershipPlan | None = None
    report_path: Path | None = None


@dataclass(frozen=True)
class MigrationPreview:
    backend: str
    database_path: Path
    revision: str
    migration_required: bool
    tasks: int
    work_items: int
    ownership: OwnershipPlan = field(default_factory=OwnershipPlan)


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
        for item_id, path in sorted(_legacy_workitem_entries(workitems_dir)):
            items_digest.update(f"{item_id}.json".encode())
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
        for item_id, path in sorted(_legacy_workitem_entries(workitems_dir)):
            shutil.copy2(path, backup_items / f"{item_id}.json")
    return backup


def _normalize_timestamps(value: object) -> object:
    if isinstance(value, datetime):
        return encode_datetime(value)
    if isinstance(value, dict):
        return {key: _normalize_timestamps(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_timestamps(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_timestamps(item) for item in value)
    if isinstance(value, set):
        return {_normalize_timestamps(item) for item in value}
    return value


def _readonly_sqlite_summary(path: Path) -> tuple[str | None, int, int, str | None]:
    """Read SQLite facts without writing sidecars or ignoring uncheckpointed WAL."""
    before = path.stat()
    if any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm")):
        raise MigrationPreflightError(
            "cannot safely preview SQLite storage while WAL sidecar files exist"
        )
    connection = sqlite3.connect(
        f"{path.absolute().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        revision_row = (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()
            if "alembic_version" in tables
            else None
        )
        revision = revision_row[0] if revision_row else None
        task_count = (
            connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            if "tasks" in tables
            else 0
        )
        work_item_count = (
            connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
            if "work_items" in tables
            else 0
        )
        ownership_row = (
            connection.execute(
                "SELECT value FROM storage_metadata WHERE key = 'ownership_plan'"
            ).fetchone()
            if "storage_metadata" in tables
            else None
        )
    except sqlite3.DatabaseError as error:
        raise MigrationPreflightError(
            f"cannot preview SQLite storage: {error}"
        ) from error
    finally:
        connection.close()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm")):
        raise MigrationPreflightError(
            "SQLite storage changed during preview; stop writers and retry"
        )
    return (
        revision,
        task_count,
        work_item_count,
        ownership_row[0] if ownership_row else None,
    )


def _ownership_audit_payload(
    *,
    plan: OwnershipPlan,
    state_fingerprint: str,
    workitems_fingerprint: str,
) -> bytes:
    return (
        json.dumps(
            {
                "input_fingerprints": {
                    "state.yaml": state_fingerprint,
                    "workitems": workitems_fingerprint,
                },
                "ownership": asdict(plan),
                "version": 1,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _write_ownership_audit(
    backup_path: Path,
    *,
    plan: OwnershipPlan,
    state_fingerprint: str,
    workitems_fingerprint: str,
) -> tuple[Path, str, str]:
    """Durably record the ownership decision alongside exact legacy bytes."""
    report_path = backup_path / "migration-report.json"
    payload = _ownership_audit_payload(
        plan=plan,
        state_fingerprint=state_fingerprint,
        workitems_fingerprint=workitems_fingerprint,
    )
    temporary = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, report_path)
        descriptor = os.open(report_path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return (
        report_path,
        hashlib.sha256(payload).hexdigest(),
        json.dumps(asdict(plan), sort_keys=True, separators=(",", ":")),
    )


def _verify_ownership_evidence(
    state_dir: Path,
    plan: OwnershipPlan,
) -> dict[str, bytes]:
    """Ensure the snapshots that informed the plan did not change under lock."""
    root = state_dir.resolve()
    payloads: dict[str, bytes] = {}
    for source, expected in plan.evidence_fingerprints.items():
        path = state_dir / source
        if not path.resolve().is_relative_to(root):
            raise MigrationPreflightError(
                f"ownership evidence {source!r} escapes the state directory"
            )
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise MigrationPreflightError(
                f"ownership evidence {source!r} is no longer readable"
            ) from error
        if hashlib.sha256(payload).hexdigest() != expected:
            raise MigrationPreflightError(
                f"ownership evidence {source!r} changed while migration was planned"
            )
        payloads[source] = payload
    return payloads


def _backup_ownership_evidence(
    backup_path: Path,
    evidence: Mapping[str, bytes],
) -> None:
    """Retain exact copies of every historical snapshot used for reconciliation."""
    evidence_dir = backup_path / "ownership-evidence"
    for source, payload in evidence.items():
        destination = evidence_dir / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)


def _resolution_conflicts(
    owner_resolutions: Mapping[str, str] | None,
    *,
    completed_plan: str | None = None,
) -> OwnershipPlan:
    """Accept an exact replay of recorded ownership choices, not new mappings."""
    if not owner_resolutions:
        return OwnershipPlan()
    if any(
        not isinstance(slug, str)
        or not slug.strip()
        or not isinstance(owner, str)
        or not owner.strip()
        for slug, owner in owner_resolutions.items()
    ):
        raise ValueError(
            "owner resolutions must map nonempty task slugs to WorkItem IDs"
        )
    recorded: dict[str, str] = {}
    if completed_plan is not None:
        try:
            for resolution in json.loads(completed_plan)["resolutions"]:
                slug, owner = resolution["task_slug"], resolution["owner_id"]
                if (
                    not isinstance(slug, str)
                    or not isinstance(owner, str)
                    or slug in recorded
                ):
                    raise ValueError("invalid recorded ownership decision")
                recorded[slug] = owner
        except (ValueError, TypeError, KeyError) as error:
            raise MigrationVerificationError(
                "recorded ownership plan is invalid"
            ) from error
    conflicts = tuple(
        OwnershipConflict(
            task_slug=slug,
            owner_ids=(),
            reason="operator mapping has no legacy task association to resolve",
            evidence=(f"operator mapping: {slug}={owner}",),
        )
        for slug, owner in sorted(owner_resolutions.items())
        if recorded.get(slug) != owner
    )
    return OwnershipPlan(conflicts=conflicts)


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
    if _normalize_timestamps(actual_state.model_dump(mode="python")) != (
        _normalize_timestamps(expected_state.model_dump(mode="python"))
    ):
        raise MigrationVerificationError(
            "candidate Task state does not match legacy state"
        )
    if _normalize_timestamps(
        {item.id: item.model_dump(mode="python") for item in actual_items}
    ) != _normalize_timestamps(
        {item.id: item.model_dump(mode="python") for item in expected_items}
    ):
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
    ownership_plan: OwnershipPlan | None = None,
    report_path: Path | None = None,
    report_fingerprint: str | None = None,
    plan_payload: str | None = None,
) -> None:
    stamp = encode_datetime(migrated_at)
    values = {
        "migrated_at": stamp,
        "legacy_state_sha256": state_fingerprint,
        "legacy_workitems_sha256": workitems_fingerprint,
    }
    if ownership_plan is not None:
        values["ownership_plan"] = plan_payload or json.dumps(
            asdict(ownership_plan),
            sort_keys=True,
            separators=(",", ":"),
        )
    if report_path is not None:
        values["migration_report_path"] = str(report_path)
    if report_fingerprint is not None:
        values["migration_report_sha256"] = report_fingerprint
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


def _revision_error(
    database: WorkspaceDatabase,
    *,
    current: str | None,
    head: str,
) -> DatabaseRevisionError:
    return DatabaseRevisionError(
        f"workspace database {database.path} is at revision {current!r}; "
        f"this binary requires {head!r}. Stop active writers and run "
        "mship state migrate with a compatible binary"
    )


def _known_packaged_revision(database: WorkspaceDatabase, revision: str) -> bool:
    scripts = ScriptDirectory.from_config(make_alembic_config(database.path))
    return revision in {
        script.revision
        for script in scripts.walk_revisions(base="base", head="heads")
        if script.revision is not None
    }


def _backup_existing_database(
    database: WorkspaceDatabase,
    *,
    revision: str,
    now: datetime,
) -> Path:
    """Create a SQLite-consistent, owner-private backup under writer exclusion."""
    backup = _unique_path(
        database.path.parent,
        f"{database.path.name}.before-{revision}-{_timestamp(now)}",
    )
    temporary = backup.with_name(f".{backup.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(
            f"{database.path.absolute().as_uri()}?mode=ro",
            uri=True,
        )
        destination = sqlite3.connect(temporary)
        source.backup(destination)
        destination.commit()
        destination.close()
        destination = None
        source.close()
        source = None
        temporary.chmod(0o600)
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, backup)
        parent_fd = os.open(backup.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
    return backup


def _upgrade_known_ancestor(
    database: WorkspaceDatabase,
    *,
    now: datetime,
    stage_hook: Callable[[str], None] | None,
) -> MigrationReport:
    """Explicitly advance one known packaged database revision under one writer lock."""
    head = database.head_revision()
    with database.write(immediate=True) as connection:
        current = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        if current == head:
            return MigrationReport(
                migrated=False,
                database_path=database.path,
                backup_path=None,
                revision=head,
                tasks=len(TaskRepository().list(connection)),
                work_items=len(
                    WorkItemRepository().list(connection, include_archived=True)
                ),
            )
        if not isinstance(current, str) or not _known_packaged_revision(
            database, current
        ):
            raise _revision_error(database, current=current, head=head)

        tasks_repo = TaskRepository()
        items_repo = WorkItemRepository()
        before_tasks = tasks_repo.list(connection)
        before_items = {
            item.id: item for item in items_repo.list(connection, include_archived=True)
        }

        _call_stage(stage_hook, "upgrade_backup")
        backup_path = _backup_existing_database(database, revision=current, now=now)
        _call_stage(stage_hook, "upgrade_alembic")
        config = make_alembic_config(database.path)
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        _call_stage(stage_hook, "upgrade_verify")

        upgraded = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        if upgraded != head:
            raise MigrationVerificationError(
                f"database upgrade ended at revision {upgraded!r}, expected {head!r}"
            )
        foreign_key_errors = connection.exec_driver_sql(
            "PRAGMA foreign_key_check"
        ).all()
        if foreign_key_errors:
            raise MigrationVerificationError(
                "database upgrade produced foreign-key integrity errors"
            )
        if (
            tasks_repo.list(connection) != before_tasks
            or {
                item.id: item
                for item in items_repo.list(connection, include_archived=True)
            }
            != before_items
        ):
            raise MigrationVerificationError(
                "database upgrade does not preserve existing workspace state"
            )

    return MigrationReport(
        migrated=True,
        database_path=database.path,
        backup_path=backup_path,
        revision=head,
        tasks=len(before_tasks),
        work_items=len(before_items),
    )


def preview_migration(
    state_dir: Path,
    *,
    owner_resolutions: Mapping[str, str] | None = None,
) -> MigrationPreview:
    """Describe a potential migration without creating files, locks, or a database."""
    state_dir = Path(state_dir)
    database = WorkspaceDatabase(state_dir)
    database_path = database.path
    head = database.head_revision()
    backend = detect_backend(state_dir)
    if backend is StorageBackend.SQLITE:
        revision, task_count, work_item_count, completed_plan = (
            _readonly_sqlite_summary(database_path)
        )
        return MigrationPreview(
            backend=backend.value,
            database_path=database_path,
            revision=revision or "unversioned",
            migration_required=revision != head,
            tasks=task_count,
            work_items=work_item_count,
            ownership=_resolution_conflicts(
                owner_resolutions, completed_plan=completed_plan
            ),
        )
    if backend is StorageBackend.LEGACY:
        legacy_state = load_legacy_state(state_dir)
        legacy_items = list_legacy_workitems(
            state_dir / "workitems",
            include_archived=True,
        )[0]
        return MigrationPreview(
            backend=backend.value,
            database_path=database_path,
            revision=head,
            migration_required=True,
            tasks=len(legacy_state.tasks),
            work_items=len(legacy_items),
            ownership=plan_ownership(
                state_dir,
                legacy_state,
                legacy_items,
                owner_resolutions=owner_resolutions,
            ),
        )
    return MigrationPreview(
        backend=backend.value,
        database_path=database_path,
        revision=head,
        migration_required=False,
        tasks=0,
        work_items=0,
        ownership=_resolution_conflicts(owner_resolutions),
    )


def migrate_state(
    state_dir: Path,
    *,
    daemon_probe: Callable[[], object | None] | None = None,
    now: datetime | None = None,
    stage_hook: Callable[[str], None] | None = None,
    owner_resolutions: Mapping[str, str] | None = None,
) -> MigrationReport:
    """Explicitly migrate legacy storage or a known packaged SQLite ancestor."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    final_database = WorkspaceDatabase(state_dir)
    migration_time = now or datetime.now(timezone.utc)
    probe = daemon_probe or daemon_is_running

    with _exclusive_lock(state_dir / "state-migration.lock"):
        if final_database.path.is_file():
            current = final_database.current_revision()
            head = final_database.head_revision()
            if current == head:
                if owner_resolutions:
                    with final_database.read() as connection:
                        completed_plan = connection.execute(
                            text(
                                "SELECT value FROM storage_metadata WHERE key = 'ownership_plan'"
                            )
                        ).scalar_one_or_none()
                    unsupported_plan = _resolution_conflicts(
                        owner_resolutions, completed_plan=completed_plan
                    )
                    if unsupported_plan.conflicts:
                        raise OwnershipConflictError(unsupported_plan)
                return _existing_report(final_database)
            unsupported_plan = _resolution_conflicts(owner_resolutions)
            if unsupported_plan.conflicts:
                raise OwnershipConflictError(unsupported_plan)
            if probe():
                raise MigrationPreflightError(
                    "the Mothership daemon is running; stop it before migration"
                )
            if current is None:
                raise _revision_error(final_database, current=current, head=head)
            return _upgrade_known_ancestor(
                final_database,
                now=migration_time,
                stage_hook=stage_hook,
            )
        if probe():
            raise MigrationPreflightError(
                "the Mothership daemon is running; stop it before migration"
            )
        backend = detect_backend(state_dir)
        if backend is StorageBackend.EMPTY:
            unsupported_plan = _resolution_conflicts(owner_resolutions)
            if unsupported_plan.conflicts:
                raise OwnershipConflictError(unsupported_plan)
            return _existing_report(final_database)
        if backend is not StorageBackend.LEGACY:
            raise MigrationPreflightError(
                f"cannot migrate storage backend {backend.value!r}"
            )

        workitems_dir = state_dir / "workitems"
        with ExitStack() as locks:
            locks.enter_context(_exclusive_lock(state_dir / "state.lock"))
            locks.enter_context(_exclusive_lock(workitems_dir / ".thread-link.lock"))
            item_paths = [
                path for _, path in sorted(_legacy_workitem_entries(workitems_dir))
            ]
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
            ownership_plan = plan_ownership(
                state_dir,
                legacy_state,
                legacy_items,
                owner_resolutions=owner_resolutions,
            )
            if ownership_plan.conflicts:
                raise OwnershipConflictError(ownership_plan)
            evidence = _verify_ownership_evidence(state_dir, ownership_plan)
            candidate_items = apply_ownership_plan(legacy_items, ownership_plan)
            state_fingerprint, items_fingerprint = _legacy_fingerprints(state_dir)

            _call_stage(stage_hook, "backup")
            backup_path = _backup_legacy(state_dir, migration_time)
            _backup_ownership_evidence(backup_path, evidence)
            _call_stage(stage_hook, "report")
            report_path, report_fingerprint, plan_payload = _write_ownership_audit(
                backup_path,
                plan=ownership_plan,
                state_fingerprint=state_fingerprint,
                workitems_fingerprint=items_fingerprint,
            )

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
                    for item in sorted(candidate_items, key=lambda value: value.id):
                        items_repo.insert(connection, item)

                _call_stage(stage_hook, "verify")
                _verify_candidate(candidate, legacy_state, candidate_items)
                _write_migration_metadata(
                    candidate,
                    migrated_at=migration_time,
                    state_fingerprint=state_fingerprint,
                    workitems_fingerprint=items_fingerprint,
                    ownership_plan=ownership_plan,
                    report_path=report_path.relative_to(state_dir),
                    report_fingerprint=report_fingerprint,
                    plan_payload=plan_payload,
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
        work_items=len(candidate_items),
        ownership=ownership_plan,
        report_path=report_path,
    )
