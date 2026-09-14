"""Immutable, private storage for declared Taskfile outputs.

This module deliberately has no knowledge of capture, run hosts, HTTP, or a
producer's command line.  The operation owner supplies a descriptor-constrained
output root plus trusted execution context; this store copies complete declared
files before that owner cleans its transient worktree.
"""
from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal, Mapping, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, field_validator

from mship.core.remote_tool import ToolContext

if TYPE_CHECKING:
    from mship.core.persistence.task_result_repository import TaskResultRepository

_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 8 * 1024
_MAX_RETENTION_SECONDS = 365 * 24 * 60 * 60
_ID = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MEDIA_TYPE = re.compile(r"^[!#$&^_.+\-0-9A-Za-z]+/[!#$&^_.+\-0-9A-Za-z]+$")
_OUTCOME = frozenset(("completed", "failed", "cancelled", "infrastructure_error"))
_AVAILABILITY = frozenset(("published", "missing", "rejected", "expired"))


class TaskResultError(RuntimeError):
    """Stable, payload-free base error for immutable result operations."""


class DeclarationError(TaskResultError, ValueError):
    """A configured declaration or producer manifest violates its contract."""


class PublicationError(TaskResultError):
    """A declared file cannot be safely made into an immutable result."""


class ResultNotFound(TaskResultError):
    """The selected result or artifact is outside the caller's scope."""


class ResultExpired(TaskResultError):
    """The selected result/artifact has passed immutable retention."""


class ResultUnavailable(TaskResultError):
    """The selected artifact has no verified private bytes."""


class ResultIntegrityError(TaskResultError):
    """A private blob is missing, replaced, truncated, or digest-invalid."""


class DeclaredOutput(BaseModel):
    """One exact producer-owned output; nested config is intentionally strict."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    relative_path: str
    name: str
    media_type: str
    diagnostic_on_failure: bool = False
    max_bytes: int = _MAX_ARTIFACT_BYTES

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if not isinstance(value, str) or not _NAME.fullmatch(value) or value in {".", ".."}:
            raise ValueError("output name must be ASCII-safe")
        return value
    @field_validator("relative_path")
    @classmethod
    def _exact_relative_path(cls, value: str) -> str:
        if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
            raise ValueError("output path must be a relative POSIX file path")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("output path contains an unsafe component")
        if any(any(character in part for character in "*?[]") for part in parts):
            raise ValueError("output path must not contain glob syntax")
        if any(":" in part for part in parts):
            raise ValueError("output path must not contain a device form")
        return value

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str) -> str:
        if not isinstance(value, str) or not _MEDIA_TYPE.fullmatch(value):
            raise ValueError("output media_type must be a concrete media type")
        return value.lower()

    @field_validator("max_bytes")
    @classmethod
    def _size_limit(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _MAX_ARTIFACT_BYTES:
            raise ValueError("output max_bytes is out of bounds")
        return value


class TaskOutputDeclaration(BaseModel):
    """Strict per-logical-task declaration, projected read-only to producers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifacts: tuple[DeclaredOutput, ...]
    retention_seconds: int
    metadata_schema: str | None = None
    metadata_version: int = 1

    @field_validator("artifacts")
    @classmethod
    def _artifacts(cls, value: tuple[DeclaredOutput, ...]) -> tuple[DeclaredOutput, ...]:
        if not value:
            raise ValueError("task output declaration requires at least one artifact")
        names = [entry.name for entry in value]
        paths = [entry.relative_path for entry in value]
        if len(set(names)) != len(names) or len(set(paths)) != len(paths):
            raise ValueError("task output artifact names and paths must be unique")
        if {"declaration.json", "manifest.json"} & set(paths):
            raise ValueError("task output path conflicts with a server-owned input")
        return value

    @field_validator("retention_seconds")
    @classmethod
    def _retention(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _MAX_RETENTION_SECONDS:
            raise ValueError("retention_seconds is out of bounds")
        return value

    @field_validator("metadata_schema")
    @classmethod
    def _metadata_schema(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not _NAME.fullmatch(value):
            raise ValueError("metadata_schema must be an ASCII-safe identifier")
        return value

    @field_validator("metadata_version")
    @classmethod
    def _metadata_version(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1024:
            raise ValueError("metadata_version is out of bounds")
        return value

    def canonical_json(self, result_id: str) -> bytes:
        """Server-owned projection binds producer output to a preallocated ID."""
        payload = self.model_dump(mode="json")
        payload["result_id"] = _safe_id(result_id, "result id")
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def parse_manifest(self, raw: bytes) -> Mapping[str, Mapping[str, object]]:
        """Accept only a bounded exact projection of the configured artifacts.

        The manifest identifies which exact already-declared files are complete;
        it cannot grant a producer new paths, names, types, IDs, or metadata.
        """
        if not isinstance(raw, bytes) or len(raw) > _MAX_MANIFEST_BYTES:
            raise DeclarationError("producer manifest is invalid")
        try:
            data = json.loads(
                raw,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise DeclarationError("producer manifest is invalid") from error
        if not isinstance(data, dict) or set(data) != {"artifacts"} or not isinstance(data["artifacts"], list):
            raise DeclarationError("producer manifest is invalid")
        entries = data["artifacts"]
        if len(entries) != len(self.artifacts):
            raise DeclarationError("producer manifest does not declare every artifact")
        parsed: dict[str, Mapping[str, object]] = {}
        for expected, entry in zip(self.artifacts, entries, strict=True):
            if not isinstance(entry, dict) or set(entry) - {"name", "relative_path", "media_type", "metadata"}:
                raise DeclarationError("producer manifest has an invalid artifact")
            if entry.get("name") != expected.name or entry.get("relative_path") != expected.relative_path or entry.get("media_type") != expected.media_type:
                raise DeclarationError("producer manifest does not match declaration")
            metadata = entry.get("metadata", {})
            try:
                encoded = json.dumps(
                    metadata, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError, RecursionError) as error:
                raise DeclarationError("producer manifest metadata is invalid") from error
            if not isinstance(metadata, dict) or metadata or len(encoded) > _MAX_METADATA_BYTES:
                raise DeclarationError("producer manifest metadata is invalid")
            parsed[expected.name] = metadata
        return parsed


@dataclass(frozen=True)
class TaskOutcome:
    status: Literal["completed", "failed", "cancelled", "infrastructure_error"]
    exit_code: int | None
    finished_at: datetime

    def __post_init__(self) -> None:
        if self.status not in _OUTCOME:
            raise ValueError("invalid task outcome")
        if self.status == "completed" and (not isinstance(self.exit_code, int) or self.exit_code != 0):
            raise ValueError("completed outcome requires exit code zero")
        if self.status == "failed" and (not isinstance(self.exit_code, int) or self.exit_code == 0):
            raise ValueError("failed outcome requires a nonzero exit code")
        if self.status in {"cancelled", "infrastructure_error"} and self.exit_code is not None:
            raise ValueError("non-producing outcome cannot expose an exit code")
        if self.finished_at.tzinfo is None:
            raise ValueError("finished_at must be timezone-aware")


@dataclass(frozen=True)
class SafeHostProvenance:
    """Trusted server-side host facts; no URL/token or caller claim is accepted."""

    host_name: str | None = None
    host_role: str | None = None
    host_endpoint_fingerprint: str | None = None

    def __post_init__(self) -> None:
        for value in (self.host_name, self.host_role, self.host_endpoint_fingerprint):
            if value is not None and (not isinstance(value, str) or not _NAME.fullmatch(value)):
                raise ValueError("invalid trusted host provenance")


@dataclass(frozen=True)
class PublishedArtifact:
    id: str
    name: str
    media_type: str
    byte_size: int | None
    sha256: str | None
    availability: Literal["published", "missing", "rejected", "expired"]
    safe_reason: str | None = None


@dataclass(frozen=True)
class TaskResult:
    id: str
    workspace_id: str
    task_slug: str
    work_item_id: str | None
    repo: str
    logical_task: str
    task_key: str
    host_name: str | None
    host_role: str | None
    host_endpoint_fingerprint: str | None
    worktree_identity: str
    source_revision: str | None
    snapshot_identity: str | None
    env_runner_identity: str | None
    outcome: TaskOutcome
    created_at: datetime
    expires_at: datetime
    artifacts: tuple[PublishedArtifact, ...]


@dataclass
class VerifiedArtifact:
    """An already pre-verified descriptor; callers must stream this exact fd."""

    fd: int
    artifact: PublishedArtifact

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "VerifiedArtifact":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _coordinated(method):
    """Serialize blob capture/metadata commit and retention across processes."""
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self._state_dir / "task-results.lock"
        with lock_path.open("a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                with self._lock:
                    return method(self, *args, **kwargs)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return wrapped


class TaskResultStore:
    """The sole private writer/reader for immutable task-result blobs."""

    def __init__(self, state_dir: Path, workspace_id: str, repository: "TaskResultRepository") -> None:
        self._state_dir = Path(state_dir)
        self._workspace_id = _safe_id(workspace_id, "workspace id")
        self._repository = repository
        self._blob_dir = self._state_dir / "task-result-blobs"
        self._lock = threading.RLock()

    @staticmethod
    def create_output_root(parent: Path) -> Path:
        """Create a unique private producer root owned by the operation owner."""
        parent = Path(parent)
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = Path(tempfile.mkdtemp(prefix="task-output-", dir=parent))
        os.chmod(root, 0o700)
        return root

    @staticmethod
    def write_declaration(
        root: Path, declaration: TaskOutputDeclaration, result_id: str
    ) -> tuple[Path, Path]:
        """Write immutable server inputs; producer only receives their paths."""
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _write_relative(
                root_fd,
                "declaration.json",
                declaration.canonical_json(result_id),
                mode=0o400,
            )
            return root / "declaration.json", root / "manifest.json"
        finally:
            os.close(root_fd)

    @_coordinated
    def publish(
        self,
        *,
        output_root: Path,
        declaration: TaskOutputDeclaration,
        context: ToolContext,
        task_key: str,
        outcome: TaskOutcome,
        result_id: str,
        work_item_id: str | None = None,
        snapshot_identity: str | None = None,
        provenance: SafeHostProvenance | None = None,
        now: datetime | None = None,
    ) -> TaskResult:
        """Copy declared files and atomically persist an immutable result manifest."""
        if not _NAME.fullmatch(context.task) or not _NAME.fullmatch(context.repo):
            raise PublicationError("invalid trusted execution context")
        task_key = _safe_logical_key(task_key)
        result_id = _safe_id(result_id, "result id")
        completed_at = (now or outcome.finished_at).astimezone(timezone.utc)
        if completed_at < outcome.finished_at.astimezone(timezone.utc) - timedelta(seconds=1):
            raise PublicationError("invalid publication time")
        root_fd = os.open(output_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            manifest = self._load_manifest(root_fd, declaration)
            artifacts: list[PublishedArtifact] = []
            locators: dict[str, str | None] = {}
            for declared in declaration.artifacts:
                artifact_id = token_urlsafe(24)
                allowed = outcome.status == "completed" or declared.diagnostic_on_failure
                if not allowed:
                    artifacts.append(PublishedArtifact(artifact_id, declared.name, declared.media_type, None, None, "missing", "not available for producer outcome"))
                    locators[artifact_id] = None
                    continue
                if manifest is None:
                    artifacts.append(PublishedArtifact(artifact_id, declared.name, declared.media_type, None, None, "rejected", "producer manifest is unavailable"))
                    locators[artifact_id] = None
                    continue
                try:
                    size, digest, locator = self._capture_relative(root_fd, declared)
                except PublicationError as error:
                    artifacts.append(PublishedArtifact(artifact_id, declared.name, declared.media_type, None, None, "rejected", _safe_reason(error)))
                    locators[artifact_id] = None
                else:
                    artifacts.append(PublishedArtifact(artifact_id, declared.name, declared.media_type, size, digest, "published"))
                    locators[artifact_id] = locator
        finally:
            os.close(root_fd)
        expires = completed_at + timedelta(seconds=declaration.retention_seconds)
        provenance = provenance or SafeHostProvenance()
        result = TaskResult(
            id=result_id, workspace_id=self._workspace_id, task_slug=context.task,
            work_item_id=work_item_id, repo=context.repo, logical_task=task_key,
            task_key=task_key, host_name=provenance.host_name, host_role=provenance.host_role,
            host_endpoint_fingerprint=provenance.host_endpoint_fingerprint,
            worktree_identity=_worktree_identity(context.worktree),
            source_revision=context.source_revision,
            snapshot_identity=snapshot_identity,
            env_runner_identity=_runner_identity(context.env_runner),
            outcome=outcome,
            created_at=completed_at,
            expires_at=expires,
            artifacts=tuple(artifacts),
        )
        self._repository.insert(result, locators)
        return result

    def list(self, *, task_slug: str | None = None, work_item_id: str | None = None, repo: str | None = None, now: datetime | None = None) -> tuple[TaskResult, ...]:
        return self._repository.list(self._workspace_id, task_slug=task_slug, work_item_id=work_item_id, repo=repo, now=now or datetime.now(timezone.utc))

    def get(self, result_id: str, *, now: datetime | None = None) -> TaskResult:
        result = self._repository.get(self._workspace_id, _safe_id(result_id, "result id"), now=now or datetime.now(timezone.utc))
        if result is None:
            raise ResultNotFound("result is unavailable")
        return result

    def open_verified(self, result_id: str, artifact_id: str, *, now: datetime | None = None) -> VerifiedArtifact:
        """Bind IDs, pre-verify exactly one pinned blob, then return its descriptor."""
        now = now or datetime.now(timezone.utc)
        result = self.get(result_id, now=now)
        if result.expires_at <= now:
            raise ResultExpired("result has expired")
        artifact_id = _safe_id(artifact_id, "artifact id")
        artifact = next((item for item in result.artifacts if item.id == artifact_id), None)
        if artifact is None:
            raise ResultNotFound("artifact is unavailable")
        if artifact.availability == "expired":
            raise ResultExpired("artifact has expired")
        if artifact.availability != "published" or artifact.sha256 is None or artifact.byte_size is None:
            raise ResultUnavailable("artifact is unavailable")
        locator = self._repository.blob_locator(self._workspace_id, result.id, artifact.id)
        if locator is None:
            raise ResultUnavailable("artifact is unavailable")
        with self._lock:
            try:
                fd = os.open(self._blob_path(locator), os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as error:
                raise ResultIntegrityError("artifact bytes are unavailable") from error
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ResultIntegrityError("artifact bytes are invalid")
                digest, count = _digest_fd(fd)
                after = os.fstat(fd)
                if (info.st_dev, info.st_ino, info.st_size) != (after.st_dev, after.st_ino, after.st_size) or count != artifact.byte_size or digest != artifact.sha256:
                    raise ResultIntegrityError("artifact bytes failed verification")
                os.lseek(fd, 0, os.SEEK_SET)
                return VerifiedArtifact(fd, artifact)
            except BaseException:
                os.close(fd)
                raise

    @_coordinated
    def expire(self, *, now: datetime | None = None) -> int:
        """Transition expired availability without mutating/deleting result metadata."""
        now = now or datetime.now(timezone.utc)
        with self._lock:
            expired = self._repository.expire(self._workspace_id, now)
            for locator in expired:
                if locator and self._repository.blob_reference_count(locator) == 0:
                    try:
                        self._blob_path(locator).unlink()
                    except FileNotFoundError:
                        pass
        return len(expired)

    def _load_manifest(self, root_fd: int, declaration: TaskOutputDeclaration) -> Mapping[str, Mapping[str, object]] | None:
        try:
            fd = _open_relative(root_fd, "manifest.json", nonblocking=True)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > _MAX_MANIFEST_BYTES
            ):
                return None
            return declaration.parse_manifest(_read_all(fd, _MAX_MANIFEST_BYTES))
        except (DeclarationError, PublicationError, OSError):
            return None
        finally:
            os.close(fd)

    def _capture_relative(self, root_fd: int, declared: DeclaredOutput) -> tuple[int, str, str]:
        try:
            fd = _open_relative(root_fd, declared.relative_path, nonblocking=True)
        except FileNotFoundError as error:
            raise PublicationError("declared output is missing") from error
        except OSError as error:
            raise PublicationError("declared output is unsafe") from error
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise PublicationError("declared output is not a regular file")
            if before.st_size < 0 or before.st_size > declared.max_bytes:
                raise PublicationError("declared output exceeds its limit")
            digest = hashlib.sha256()
            self._blob_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp_fd, temp_name = tempfile.mkstemp(prefix=".pending-", dir=self._blob_dir)
            try:
                os.fchmod(temp_fd, 0o600)
                count = 0
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > declared.max_bytes:
                        raise PublicationError("declared output exceeds its limit")
                    digest.update(chunk)
                    _write_all(temp_fd, chunk)
                os.fsync(temp_fd)
                after = os.fstat(fd)
                if (
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or count != before.st_size
                ):
                    raise PublicationError("declared output changed during copy")
                hex_digest = digest.hexdigest()
                locator = f"{hex_digest[:2]}/{hex_digest}"
                destination = self._blob_path(locator)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    os.link(temp_name, destination)
                except FileExistsError:
                    existing_fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        existing_info = os.fstat(existing_fd)
                        if not stat.S_ISREG(existing_info.st_mode) or existing_info.st_nlink != 1:
                            raise PublicationError("immutable blob is invalid")
                        existing_digest, existing_size = _digest_fd(existing_fd)
                    finally:
                        os.close(existing_fd)
                    if existing_digest != hex_digest or existing_size != count:
                        raise PublicationError("immutable blob is invalid")
                else:
                    _fsync_directory(destination.parent)
                return count, hex_digest, locator
            finally:
                os.close(temp_fd)
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        finally:
            os.close(fd)

    def _blob_path(self, locator: str) -> Path:
        parts = locator.split("/")
        if len(parts) != 2 or len(parts[0]) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[1]):
            raise ResultIntegrityError("artifact locator is invalid")
        return self._blob_dir / parts[0] / parts[1]

def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")



def _safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _safe_logical_key(value: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("invalid task key")
    return value


def _runner_identity(value: str | None) -> str | None:
    return None if value is None else hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_reason(error: Exception) -> str:
    message = str(error)
    return message if message in {
        "declared output is missing", "declared output is not a regular file",
        "declared output exceeds its limit", "declared output has unsafe permissions",
        "declared output changed during copy", "declared output is unsafe",
        "immutable blob is invalid",
    } else "declared output was rejected"


def _worktree_identity(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _open_relative(root_fd: int, relative: str, *, nonblocking: bool = False) -> int:
    parts = relative.split("/")
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_NONBLOCK if nonblocking else 0)
        return os.open(parts[-1], flags, dir_fd=fd)
    finally:
        os.close(fd)


def _write_relative(
    root_fd: int, relative: str, content: bytes, *, mode: int = 0o600
) -> None:
    fd = os.open(
        relative,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=root_fd,
    )
    try:
        _write_all(fd, content)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        offset += os.write(fd, content[offset:])


def _read_all(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(64 * 1024, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise PublicationError("producer manifest is too large")
        chunks.append(chunk)


def _digest_fd(fd: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return digest.hexdigest(), count
        digest.update(chunk)
        count += len(chunk)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
