"""Durable, safe selected-target metadata; never an execution owner."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from secrets import token_urlsafe

from sqlalchemy import Connection, delete, select

from mship.core.persistence.schema import app_runs
from mship.core.persistence.serialization import (
    PersistenceDecodeError,
    decode_datetime,
    decode_json,
    encode_datetime,
    encode_json,
)
from mship.core.run_target.models import AppRun, JsonValue

_BINDING_REF = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_MAX_BINDING_BYTES = 256 * 1024
_CANDIDATE_STATUSES = ("starting", "active", "unknown")
_TERMINAL_STATUSES = frozenset(("stopped", "failed"))
_ALLOWED_TRANSITIONS = {
    "starting": frozenset(("active", "unknown", "stopped", "failed")),
    "active": frozenset(("unknown", "stopped", "failed")),
    "unknown": frozenset(("stopped", "failed")),
    "stopped": frozenset(),
    "failed": frozenset(),
}


class AppRunConflict(RuntimeError):
    """A transition's expected revision is stale."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"app run {run_id!r} changed concurrently; refresh before retrying")


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
        row = connection.execute(
            select(app_runs).where(app_runs.c.id == run_id)
        ).mappings().one_or_none()
        return None if row is None else self._decode(row)

    def list_active(
        self,
        connection: Connection,
        *,
        task_slug: str,
        repo: str,
    ) -> list[AppRun]:
        """Return exact task/repo observation candidates, including uncertain runs.

        The historical name is retained by the planned API. It deliberately includes
        ``starting`` and ``unknown`` records: neither implies liveness, but removing
        either could falsely make a different run uniquely selectable.
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
        updated = self.get(connection, run_id)
        if updated is None:  # pragma: no cover - guarded by the successful UPDATE.
            raise KeyError(run_id)
        return updated

    def delete_for_task(self, connection: Connection, task_slug: str) -> None:
        """Remove terminal metadata only after the owner/evidence lifecycle permits it.

        This repository refuses to erase ``starting``, ``active``, or ``unknown`` rows.
        Task deletion therefore cannot cascade away possibly-live/uncertain metadata;
        lifecycle owners must first confirm a terminal result and explicitly call here.
        """
        rows = connection.execute(
            select(app_runs.c.private_binding_ref, app_runs.c.status).where(
                app_runs.c.task_slug == task_slug
            )
        ).mappings().all()
        if any(str(row["status"]) not in _TERMINAL_STATUSES for row in rows):
            raise AppRunCleanupBlocked(task_slug)
        connection.execute(delete(app_runs).where(app_runs.c.task_slug == task_slug))
        for row in rows:
            self._binding_path(str(row["private_binding_ref"])).unlink(missing_ok=True)

    def store_private_binding(self, binding: Mapping[str, JsonValue]) -> str:
        """Atomically create and return an unguessable owner-private binding key."""
        encoded = json.dumps(dict(binding), sort_keys=True, separators=(",", ":")).encode()
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
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
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
        if status == "active":
            if not owner_ref or not owner_generation:
                raise AppRunTransitionError("active app run acknowledgement requires owner identity")
        elif owner_ref is not None or owner_generation is not None:
            raise AppRunTransitionError(
                f"{status} app run acknowledgement must not retain an owner reference"
            )

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
            "owner_ref": run.owner_ref,
            "owner_generation": run.owner_generation,
            "status": run.status,
            "revision": run.revision,
            "created_at": encode_datetime(run.created_at),
            "updated_at": encode_datetime(run.updated_at),
            "binary_provenance_json": (
                None
                if run.binary_provenance is None
                else encode_json(run.binary_provenance)
            ),
        }

    def _decode(self, row: Mapping[str, object]) -> AppRun:
        try:
            capabilities = decode_json(str(row["capabilities_json"]))
            provenance_text = row["binary_provenance_json"]
            provenance = (
                None if provenance_text is None else decode_json(str(provenance_text))
            )
            if not isinstance(capabilities, list) or not isinstance(provenance, (dict, type(None))):
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
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PersistenceDecodeError("app_runs", str(row.get("id", "unknown")), error) from error
