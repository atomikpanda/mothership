"""Durable, safe selected-target metadata; never an execution owner."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from secrets import token_urlsafe

from sqlalchemy import Connection, delete, select

from mship.core.persistence.schema import app_runs
from mship.core.persistence.serialization import (
    PersistenceDecodeError,
    decode_datetime,
    decode_json,
    decode_source_update_receipt,
    encode_datetime,
    encode_json,
    encode_source_update_receipt,
)
from mship.core.run_target.models import (
    AppRun,
    JsonValue,
    _validate_source_update_receipt,
)

_BINDING_REF = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_MAX_BINDING_BYTES = 256 * 1024
_CANDIDATE_STATUSES = ("starting", "active", "updating", "unknown")
_TERMINAL_STATUSES = frozenset(("stopped", "failed"))
_ALLOWED_TRANSITIONS = {
    "starting": frozenset(("starting", "active", "unknown", "stopped", "failed")),
    "active": frozenset(("updating", "unknown", "stopped", "failed")),
    "updating": frozenset(("active", "unknown", "stopped", "failed")),
    "unknown": frozenset(("stopped", "failed")),
    "stopped": frozenset(),
    "failed": frozenset(),
}
_SOURCE_UPDATE_ORDER = {
    "reserved": 0,
    "source-applied": 1,
    "context-committed": 2,
    "reloaded": 3,
    "unknown": 4,
}


class AppRunConflict(RuntimeError):
    """A transition's expected revision is stale."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(
            f"app run {run_id!r} changed concurrently; refresh before retrying"
        )


class AppRunTransitionError(ValueError):
    """A requested metadata transition is not a legal owner acknowledgement."""


class AppRunCleanupBlocked(RuntimeError):
    """Selected metadata may not be discarded while its outcome is uncertain."""

    def __init__(self, task_slug: str) -> None:
        self.task_slug = task_slug
        super().__init__(
            f"cannot delete app-run metadata for task {task_slug!r} while a run is "
            "starting, active, or unknown"
        )


class PrivateBindingError(RuntimeError):
    """A private binding cannot be safely created or read."""


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    parent_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


class AppRunRepository:
    """Persist selected context safely at the workspace transaction boundary.

    Private bindings live beside the canonical workspace database, but outside SQLite
    and every public projection. Their opaque keys are generated only here.
    """

    def __init__(self, state_dir: Path) -> None:
        self._binding_dir = Path(state_dir) / "app-run-bindings"

    def insert(self, connection: Connection, run: AppRun) -> None:
        self._validate_binding_ref(run.private_binding_ref)
        if not self._binding_path(run.private_binding_ref).exists():
            raise PrivateBindingError("private binding is missing")
        connection.execute(app_runs.insert().values(**self._values(run)))

    def get(self, connection: Connection, run_id: str) -> AppRun | None:
        row = (
            connection.execute(select(app_runs).where(app_runs.c.id == run_id))
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._decode(row)

    def list_for_task(self, connection: Connection, *, task_slug: str) -> list[AppRun]:
        """Return every task run, including retained terminal recovery evidence."""
        rows = connection.execute(
            select(app_runs)
            .where(app_runs.c.task_slug == task_slug)
            .order_by(app_runs.c.created_at, app_runs.c.id)
        ).mappings()
        return [self._decode(row) for row in rows]

    def list_candidates(
        self,
        connection: Connection,
        *,
        task_slug: str,
        repo: str,
    ) -> list[AppRun]:
        """Return exact task/repo observation candidates, including uncertainty.

        ``starting`` and ``unknown`` never assert liveness, but removing either
        could falsely make a different run uniquely selectable.
        """
        rows = connection.execute(
            select(app_runs)
            .where(
                app_runs.c.task_slug == task_slug,
                app_runs.c.repo == repo,
                app_runs.c.status.in_(_CANDIDATE_STATUSES),
            )
            .order_by(app_runs.c.created_at, app_runs.c.id)
        ).mappings()
        return [self._decode(row) for row in rows]

    def transition(
        self,
        connection: Connection,
        *,
        run_id: str,
        expected_revision: int,
        status: str,
        owner_ref: str | None,
        owner_generation: str | None,
        now: datetime,
    ) -> AppRun:
        current = self.get(connection, run_id)
        if current is None:
            raise KeyError(run_id)
        if current.revision != expected_revision:
            raise AppRunConflict(run_id)
        self._validate_transition(current, status, owner_ref, owner_generation)
        try:
            updated = replace(
                current,
                status=status,
                owner_ref=owner_ref,
                owner_generation=owner_generation,
                revision=expected_revision + 1,
                updated_at=now,
            )
        except ValueError as error:
            raise AppRunTransitionError(str(error)) from error
        statement = (
            app_runs.update()
            .where(app_runs.c.id == run_id, app_runs.c.revision == expected_revision)
            .values(
                status=status,
                owner_ref=owner_ref,
                owner_generation=owner_generation,
                revision=expected_revision + 1,
                updated_at=encode_datetime(now),
            )
        )
        if connection.execute(statement).rowcount != 1:
            raise AppRunConflict(run_id)
        return updated

    def record_source_update(
        self,
        connection: Connection,
        *,
        run_id: str,
        expected_revision: int,
        receipt: Mapping[str, JsonValue],
        now: datetime,
    ) -> AppRun:
        """Durably record staged evidence without claiming a source switch."""
        current = self._current_for_source_update(
            connection, run_id, expected_revision, receipt
        )
        if current.status not in {"active", "updating"}:
            raise AppRunTransitionError("source updates require an active run")
        return self._cas_source_update(
            connection,
            current=current,
            expected_revision=expected_revision,
            receipt=receipt,
            now=now,
        )

    def acknowledge_source_update(
        self,
        connection: Connection,
        *,
        run_id: str,
        expected_revision: int,
        receipt: Mapping[str, JsonValue],
        new_profile_revision: str,
        now: datetime,
    ) -> AppRun:
        """CAS the certified source/profile identity before the remote owner commits."""
        current = self._current_for_source_update(
            connection, run_id, expected_revision, receipt
        )
        receipt_values = self._receipt_values(receipt)
        if (
            current.status != "active"
            or receipt_values["stage"] != "source-applied"
            or current.backend_revision != receipt_values["old_source_revision"]
        ):
            raise AppRunTransitionError("source update acknowledgement is invalid")
        try:
            updated = replace(
                current,
                backend_revision=receipt_values["new_source_revision"],
                profile_revision=new_profile_revision,
                status="updating",
                source_update_receipt=receipt_values,
                revision=expected_revision + 1,
                updated_at=now,
            )
        except ValueError as error:
            raise AppRunTransitionError(str(error)) from error
        statement = (
            app_runs.update()
            .where(app_runs.c.id == run_id, app_runs.c.revision == expected_revision)
            .values(
                backend_revision=updated.backend_revision,
                profile_revision=updated.profile_revision,
                status=updated.status,
                source_update_receipt_json=encode_source_update_receipt(receipt_values),
                revision=updated.revision,
                updated_at=encode_datetime(now),
            )
        )
        if connection.execute(statement).rowcount != 1:
            raise AppRunConflict(run_id)
        return updated

    def finalize_source_update(
        self,
        connection: Connection,
        *,
        run_id: str,
        expected_revision: int,
        receipt: Mapping[str, JsonValue],
        now: datetime,
    ) -> AppRun:
        """Acknowledge one verified reload without replaying it."""
        current = self._current_for_source_update(
            connection, run_id, expected_revision, receipt
        )
        receipt_values = self._receipt_values(receipt)
        if (
            current.status != "updating"
            or receipt_values["stage"] != "reloaded"
            or current.backend_revision != receipt_values["new_source_revision"]
        ):
            raise AppRunTransitionError("source update completion is invalid")
        return self._cas_source_update(
            connection,
            current=current,
            expected_revision=expected_revision,
            receipt=receipt_values,
            status="active",
            now=now,
        )

    def mark_source_update_unknown(
        self,
        connection: Connection,
        *,
        run_id: str,
        expected_revision: int,
        receipt: Mapping[str, JsonValue],
        now: datetime,
    ) -> AppRun:
        """Retain partial evidence and block normal observation after uncertainty."""
        current = self._current_for_source_update(
            connection, run_id, expected_revision, receipt
        )
        receipt_values = self._receipt_values(receipt)
        if receipt_values["stage"] != "unknown" or current.status not in {
            "active",
            "updating",
        }:
            raise AppRunTransitionError("source update uncertainty is invalid")
        return self._cas_source_update(
            connection,
            current=current,
            expected_revision=expected_revision,
            receipt=receipt_values,
            status="unknown",
            now=now,
        )

    def delete_for_task(
        self, connection: Connection, task_slug: str
    ) -> tuple[str, ...]:
        """Delete terminal metadata and hand private refs to post-commit cleanup.

        The returned opaque refs are not authorization to remove payload files.
        The task-close facade commits this deletion first, then opens a separate
        write transaction to prove no surviving row references each payload.
        """
        rows = (
            connection.execute(
                select(app_runs.c.private_binding_ref, app_runs.c.status).where(
                    app_runs.c.task_slug == task_slug
                )
            )
            .mappings()
            .all()
        )
        if any(str(row["status"]) not in _TERMINAL_STATUSES for row in rows):
            raise AppRunCleanupBlocked(task_slug)
        connection.execute(delete(app_runs).where(app_runs.c.task_slug == task_slug))
        return tuple(dict.fromkeys(str(row["private_binding_ref"]) for row in rows))

    def remove_unreferenced_private_bindings(
        self, connection: Connection, binding_refs: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Remove only post-commit handoffs no surviving AppRun still references."""
        unique_refs = tuple(dict.fromkeys(binding_refs))
        for binding_ref in unique_refs:
            self._validate_binding_ref(binding_ref)
        if not unique_refs:
            return ()
        referenced = {
            str(row["private_binding_ref"])
            for row in connection.execute(
                select(app_runs.c.private_binding_ref).where(
                    app_runs.c.private_binding_ref.in_(unique_refs)
                )
            ).mappings()
        }
        removed: list[str] = []
        for binding_ref in unique_refs:
            if binding_ref in referenced:
                continue
            path = self._binding_path(binding_ref)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(binding_ref)
        return tuple(removed)

    def store_private_binding(self, binding: Mapping[str, JsonValue]) -> str:
        """Atomically create and return an unguessable owner-private binding key."""
        encoded = json.dumps(
            dict(binding), sort_keys=True, separators=(",", ":")
        ).encode()
        if len(encoded) > _MAX_BINDING_BYTES:
            raise PrivateBindingError("private binding exceeds the bounded size")
        _private_dir(self._binding_dir)
        while True:
            ref = token_urlsafe(32)
            path = self._binding_path(ref)
            try:
                self._atomic_create(path, encoded)
            except FileExistsError:
                continue
            return ref

    def load_private_binding(self, binding_ref: str) -> dict[str, JsonValue]:
        """Load one regular, owner-private, bounded private binding file."""
        self._validate_binding_ref(binding_ref)
        path = self._binding_path(binding_ref)
        try:
            info = path.lstat()
        except OSError as error:
            raise PrivateBindingError("private binding is unavailable") from error
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise PrivateBindingError("private binding is not a regular file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PrivateBindingError("private binding is not owner-private")
        if info.st_size > _MAX_BINDING_BYTES:
            raise PrivateBindingError("private binding exceeds the bounded size")
        try:
            value = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            raise PrivateBindingError("private binding is unreadable") from error
        if not isinstance(value, dict) or not all(
            isinstance(key, str) for key in value
        ):
            raise PrivateBindingError("private binding has invalid data")
        return value  # type: ignore[return-value]

    def _atomic_create(self, path: Path, content: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".binding-", dir=self._binding_dir)
        temp = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        else:
            temp.unlink(missing_ok=True)
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    def _binding_path(self, binding_ref: str) -> Path:
        return self._binding_dir / f"{binding_ref}.json"

    def _validate_binding_ref(self, binding_ref: str) -> None:
        if not _BINDING_REF.fullmatch(binding_ref):
            raise PrivateBindingError("private binding reference is invalid")

    def _validate_transition(
        self,
        current: AppRun,
        status: str,
        owner_ref: str | None,
        owner_generation: str | None,
    ) -> None:
        if status not in _ALLOWED_TRANSITIONS.get(current.status, frozenset()):
            raise AppRunTransitionError(
                f"cannot transition app run from {current.status!r} to {status!r}"
            )
        if (owner_ref is None) != (owner_generation is None):
            raise AppRunTransitionError(
                "owner reference and generation must be supplied together"
            )
        if status in {"active", "updating"} and owner_ref is None:
            raise AppRunTransitionError(
                "active or updating app run acknowledgement requires owner identity"
            )
        if current.owner_ref is not None and (
            owner_ref != current.owner_ref
            or owner_generation != current.owner_generation
        ):
            raise AppRunTransitionError(
                "acknowledged app run owner identity cannot be cleared or replaced"
            )
        if (
            current.owner_ref is None
            and status not in {"starting", "active", "updating"}
            and owner_ref is not None
        ):
            raise AppRunTransitionError(
                "owner identity may only be acknowledged by starting or active transition"
            )

    def _receipt_values(self, receipt: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        if not isinstance(receipt, Mapping):
            raise AppRunTransitionError("source update receipt is invalid")
        values = dict(receipt)
        try:
            _validate_source_update_receipt(values)
        except ValueError as error:
            raise AppRunTransitionError(str(error)) from error
        return values

    def _current_for_source_update(
        self,
        connection: Connection,
        run_id: str,
        expected_revision: int,
        receipt: Mapping[str, JsonValue],
    ) -> AppRun:
        current = self.get(connection, run_id)
        if current is None:
            raise KeyError(run_id)
        if current.revision != expected_revision:
            raise AppRunConflict(run_id)
        values = self._receipt_values(receipt)
        if (
            current.owner_ref != values["owner_ref"]
            or current.owner_generation != values["generation"]
        ):
            raise AppRunTransitionError("source update owner identity is invalid")
        existing = current.source_update_receipt
        if existing is not None:
            if existing["update_id"] != values["update_id"]:
                if existing["stage"] != "reloaded":
                    raise AppRunTransitionError(
                        "another source update is already recorded"
                    )
            else:
                if any(
                    existing[field] != values[field]
                    for field in (
                        "owner_ref",
                        "generation",
                        "old_source_revision",
                        "new_source_revision",
                    )
                ):
                    raise AppRunTransitionError(
                        "source update receipt identity changed"
                    )
                if (
                    existing["stage"] == "unknown"
                    or values["stage"] != "unknown"
                    and _SOURCE_UPDATE_ORDER[values["stage"]]
                    < _SOURCE_UPDATE_ORDER[existing["stage"]]
                ):
                    raise AppRunTransitionError("source update receipt regressed")
        return current

    def _cas_source_update(
        self,
        connection: Connection,
        *,
        current: AppRun,
        expected_revision: int,
        receipt: Mapping[str, JsonValue],
        now: datetime,
        status: str | None = None,
    ) -> AppRun:
        receipt_values = self._receipt_values(receipt)
        try:
            updated = replace(
                current,
                status=current.status if status is None else status,
                source_update_receipt=receipt_values,
                revision=expected_revision + 1,
                updated_at=now,
            )
        except ValueError as error:
            raise AppRunTransitionError(str(error)) from error
        statement = (
            app_runs.update()
            .where(
                app_runs.c.id == current.id,
                app_runs.c.revision == expected_revision,
            )
            .values(
                status=updated.status,
                source_update_receipt_json=encode_source_update_receipt(receipt_values),
                revision=updated.revision,
                updated_at=encode_datetime(now),
            )
        )
        if connection.execute(statement).rowcount != 1:
            raise AppRunConflict(current.id)
        return updated

    def _values(self, run: AppRun) -> dict[str, object]:
        return {
            "id": run.id,
            "task_slug": run.task_slug,
            "repo": run.repo,
            "profile": run.profile,
            "profile_revision": run.profile_revision,
            "backend": run.backend,
            "backend_revision": run.backend_revision,
            "host_name": run.host_name,
            "host_scope": run.host_scope,
            "host_endpoint_fingerprint": run.host_endpoint_fingerprint,
            "safe_target_label": run.safe_target_label,
            "private_binding_ref": run.private_binding_ref,
            "operation": run.operation,
            "protocol_version": run.protocol_version,
            "capabilities_json": encode_json(run.capabilities),
            "target_aliases_json": encode_json(run.target_aliases),
            "owner_ref": run.owner_ref,
            "owner_generation": run.owner_generation,
            "status": run.status,
            "revision": run.revision,
            "created_at": encode_datetime(run.created_at),
            "updated_at": encode_datetime(run.updated_at),
            "binary_provenance_json": None,
            "source_update_receipt_json": encode_source_update_receipt(
                run.source_update_receipt
            ),
        }

    def _decode(self, row: Mapping[str, object]) -> AppRun:
        try:
            capabilities = decode_json(str(row["capabilities_json"]))
            target_aliases = decode_json(str(row["target_aliases_json"]))
            provenance_text = row["binary_provenance_json"]
            provenance = (
                None if provenance_text is None else decode_json(str(provenance_text))
            )
            source_update_receipt = decode_source_update_receipt(
                None
                if row["source_update_receipt_json"] is None
                else str(row["source_update_receipt_json"])
            )
            if (
                not isinstance(capabilities, list)
                or not isinstance(target_aliases, list)
                or not isinstance(provenance, (dict, type(None)))
                or not isinstance(source_update_receipt, (dict, type(None)))
            ):
                raise ValueError("invalid JSON field")
            return AppRun(
                id=str(row["id"]),
                task_slug=str(row["task_slug"]),
                repo=str(row["repo"]),
                profile=str(row["profile"]),
                profile_revision=str(row["profile_revision"]),
                backend=str(row["backend"]),
                backend_revision=str(row["backend_revision"]),
                host_name=str(row["host_name"]),
                host_scope=str(row["host_scope"]),  # type: ignore[arg-type]
                host_endpoint_fingerprint=str(row["host_endpoint_fingerprint"]),
                safe_target_label=str(row["safe_target_label"]),
                private_binding_ref=str(row["private_binding_ref"]),
                operation=str(row["operation"]),
                protocol_version=int(row["protocol_version"]),  # type: ignore[arg-type]
                capabilities=tuple(str(value) for value in capabilities),
                target_aliases=tuple(str(value) for value in target_aliases),
                owner_ref=None if row["owner_ref"] is None else str(row["owner_ref"]),
                owner_generation=(
                    None
                    if row["owner_generation"] is None
                    else str(row["owner_generation"])
                ),
                status=str(row["status"]),  # type: ignore[arg-type]
                revision=int(row["revision"]),
                created_at=decode_datetime(str(row["created_at"])),  # type: ignore[arg-type]
                updated_at=decode_datetime(str(row["updated_at"])),  # type: ignore[arg-type]
                binary_provenance=provenance,  # type: ignore[arg-type]
                source_update_receipt=source_update_receipt,  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PersistenceDecodeError(
                "app_runs", str(row.get("id", "unknown")), error
            ) from error
