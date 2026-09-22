"""Strict data contracts for profile-driven target discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import math
import re
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mship.core.capture import Artifact
from mship.core.run_host.config import HostRegistration
from mship.core.run_target.builtins import BUILTIN_BACKENDS, BUILTIN_TASK_PREFIX

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)

_MAX_JSON_DEPTH = 16
_MAX_JSON_ITEMS = 512
_MAX_JSON_STRING = 16 * 1024


def _json_value(
    value: object, *, depth: int = 0, items: list[int] | None = None
) -> JsonValue:
    """Validate bounded JSON data without allowing executable configuration values."""
    if items is None:
        items = [0]
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON-compatible data is nested too deeply")
    items[0] += 1
    if items[0] > _MAX_JSON_ITEMS:
        raise ValueError("JSON-compatible data has too many values")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_JSON_STRING:
            raise ValueError("JSON-compatible string is too long")
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON-compatible numbers must be finite")
        return value
    if isinstance(value, list):
        return [_json_value(item, depth=depth + 1, items=items) for item in value]
    if isinstance(value, dict):
        converted: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(
                    "JSON-compatible mappings require non-empty string keys"
                )
            converted[key] = _json_value(item, depth=depth + 1, items=items)
        return converted
    raise ValueError("value is not JSON-compatible data")


def _safe_text(value: str, *, field: str) -> str:
    if not value or len(value) > 1024 or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} must be non-empty safe text")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def safe_identifier(value: str, *, field: str) -> str:
    """Validate a user-facing profile/backend identifier consistently."""
    return _safe_text(value, field=field)


class HostRequirements(_StrictModel):
    roles: tuple[str, ...]
    tags: tuple[str, ...] = ()

    @field_validator("roles", "tags")
    @classmethod
    def validate_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("host requirements must contain non-empty names")
        if len(set(values)) != len(values):
            raise ValueError("host requirements must not contain duplicates")
        return values


class RunProfile(_StrictModel):
    backend: str
    hosts: HostRequirements
    options: dict[str, Any]

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, value: str) -> str:
        return _safe_text(value, field="backend")

    @field_validator("options")
    @classmethod
    def validate_options(cls, value: dict[str, Any]) -> dict[str, JsonValue]:
        return _json_value(value)  # type: ignore[return-value]


class BackendConfig(_StrictModel):
    builtin: Literal["android", "flutter", "ios", "browser", "platformio"] | None = None
    discover_task: str
    operations: dict[str, str]
    session_owner: Literal["android", "flutter"] | None = None

    @model_validator(mode="before")
    @classmethod
    def inherit_builtin(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("builtin") is None:
            return value
        name = value["builtin"]
        if not isinstance(name, str) or name not in BUILTIN_BACKENDS:
            raise ValueError("unknown integrated backend")
        builtin = BUILTIN_BACKENDS[name]
        if value.get("session_owner", builtin.session_owner) != builtin.session_owner:
            raise ValueError("integrated backend session ownership cannot be changed")
        overrides = value.get("operations", {})
        if not isinstance(overrides, dict):
            raise ValueError("backend operations must be a mapping")
        return {
            **value,
            "session_owner": builtin.session_owner,
            "discover_task": value.get("discover_task", builtin.task_key("discover")),
            "operations": {
                **{
                    operation: builtin.task_key(operation)
                    for operation in builtin.operations
                },
                **overrides,
            },
        }

    @model_validator(mode="after")
    def validate_builtin_tasks(self) -> "BackendConfig":
        builtin = (
            BUILTIN_BACKENDS.get(self.builtin) if self.builtin is not None else None
        )
        for operation, task in ((None, self.discover_task), *self.operations.items()):
            if task.startswith(BUILTIN_TASK_PREFIX) and (
                builtin is None
                or (operation is not None and operation not in builtin.operations)
                or task != builtin.task_key(operation or "discover")
            ):
                raise ValueError(
                    "integrated task key does not match its backend operation"
                )
        return self

    def builtin_operation(self, task_key: str) -> str | None:
        """Resolve only inherited actions, never a project task override."""
        if self.builtin is None:
            return None
        builtin = BUILTIN_BACKENDS[self.builtin]
        if task_key == self.discover_task == builtin.task_key("discover"):
            return "discover"
        for operation in builtin.operations:
            if task_key == self.operations[operation] == builtin.task_key(operation):
                return operation
        return None

    @field_validator("discover_task")
    @classmethod
    def validate_discover_task(cls, value: str) -> str:
        return _safe_text(value, field="discover_task")

    @field_validator("operations")
    @classmethod
    def validate_operations(cls, value: dict[str, str]) -> dict[str, str]:
        for operation, task in value.items():
            _safe_text(operation, field="operation")
            _safe_text(task, field="operation task")
        return value


class DiscoveryRequest(_StrictModel):
    protocol_version: Literal[1]
    backend: str
    backend_revision: str
    profile: str
    profile_revision: str
    task: str
    repo: str
    operation: str
    options: dict[str, Any]
    target_alias: str | None

    @field_validator(
        "backend",
        "backend_revision",
        "profile",
        "profile_revision",
        "task",
        "repo",
        "operation",
    )
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _safe_text(value, field="discovery request identifier")

    @field_validator("target_alias")
    @classmethod
    def validate_alias(cls, value: str | None) -> str | None:
        return None if value is None else _safe_text(value, field="target_alias")

    @field_validator("options")
    @classmethod
    def validate_options(cls, value: dict[str, Any]) -> dict[str, JsonValue]:
        return _json_value(value)  # type: ignore[return-value]


class TargetCandidate(_StrictModel):
    target_key: str
    label: str
    tags: tuple[str, ...]
    roles: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    capabilities: tuple[str, ...]
    ready: bool
    reason: str | None
    remediation: str | None
    preparation: tuple[str, ...]
    rank: tuple[int, ...]
    binding: dict[str, Any]

    @field_validator("target_key", "label")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        return _safe_text(value, field="candidate field")

    @field_validator("tags", "roles", "aliases", "capabilities", "preparation")
    @classmethod
    def validate_text_lists(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("candidate metadata must contain non-empty strings")
        if len(set(values)) != len(values):
            raise ValueError("candidate metadata must not contain duplicates")
        return values

    @field_validator("rank", mode="before")
    @classmethod
    def validate_rank(cls, values: object) -> object:
        if not isinstance(values, (list, tuple)) or any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError("rank values must be integers")
        return values

    @field_validator("binding")
    @classmethod
    def validate_binding(cls, value: dict[str, Any]) -> dict[str, JsonValue]:
        return _json_value(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def validate_readiness(self) -> "TargetCandidate":
        if self.ready and (self.reason is not None or self.remediation is not None):
            raise ValueError("ready candidates cannot report a reason or remediation")
        if not self.ready and self.reason is None:
            raise ValueError("unready candidates require a reason")
        return self


class BackendError(_StrictModel):
    code: str
    message: str
    remediation: str | None

    @field_validator("code", "message")
    @classmethod
    def validate_error_text(cls, value: str) -> str:
        return _safe_text(value, field="backend error")

    @field_validator("remediation")
    @classmethod
    def validate_error_remediation(cls, value: str | None) -> str | None:
        return None if value is None else _safe_text(value, field="backend remediation")


class DiscoveryResult(_StrictModel):
    protocol_version: Literal[1]
    backend: str
    backend_revision: str
    rank_schema: tuple[str, ...]
    candidates: tuple[TargetCandidate, ...]
    errors: tuple[BackendError, ...]

    @field_validator("backend", "backend_revision")
    @classmethod
    def validate_result_identifiers(cls, value: str) -> str:
        return _safe_text(value, field="discovery result identifier")

    @field_validator("rank_schema")
    @classmethod
    def validate_rank_schema(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("rank schema must contain non-empty column names")
        if len(set(values)) != len(values):
            raise ValueError("rank schema must not contain duplicates")
        return values


@dataclass(frozen=True)
class HostInventory:
    host: HostRegistration
    backend_revision: str
    rank_schema: tuple[str, ...]
    operation: str
    profile_revision: str
    candidates: tuple[TargetCandidate, ...]
    error: str | None


@dataclass(frozen=True)
class SelectedTarget:
    host: HostRegistration
    candidate: TargetCandidate
    backend_revision: str
    rank_schema: tuple[str, ...]
    profile_revision: str


@dataclass(frozen=True)
class BackendExecution:
    """Owner-side execution policy, not backend-controlled request-file data.

    Discovery executors must enforce the byte caps during collection and the
    timeout across execution, stopping their owned operation on either limit.
    Launch/observe output is streamed to the owner sink, never buffered without
    a cap; its byte-cap fields are therefore None.
    """

    task: str
    repo: str
    profile: str
    backend: str
    logical_task: str
    operation: str
    request: dict[str, JsonValue]
    run_id: str | None
    preparation: Literal["discover", "launch", "observe"]
    max_stdout_bytes: int | None
    max_stderr_bytes: int | None
    timeout_seconds: float | None

    def __post_init__(self) -> None:
        if self.preparation not in {"discover", "launch", "observe"}:
            raise ValueError("invalid backend preparation policy")
        if self.preparation == "discover":
            for limit in (self.max_stdout_bytes, self.max_stderr_bytes):
                if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                    raise ValueError("discovery requires positive output byte caps")
            if self.timeout_seconds is None:
                raise ValueError("discovery requires a timeout")
        elif self.max_stdout_bytes is not None or self.max_stderr_bytes is not None:
            raise ValueError("launch and observe output must use the streaming sink")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("execution timeout must be finite and positive")


@dataclass(frozen=True)
class BackendResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    error_code: str | None
    owner_ref: str | None
    owner_generation: str | None
    artifacts: tuple[Artifact, ...]


class TargetSelectionError(Exception):
    def __init__(self, code: str, message: str, details: tuple[str, ...] = ()) -> None:
        self.code = code
        self.message = _safe_text(message, field="selection error")
        self.details = tuple(
            _safe_text(detail, field="selection detail") for detail in details
        )
        super().__init__(self.message)


def profile_revision(
    profile: RunProfile, backend: BackendConfig, *, prepared_source_revision: str
) -> str:
    """Fingerprint the effective definition and immutable source snapshot."""
    source = _safe_text(prepared_source_revision, field="prepared source revision")
    payload = {
        "backend": backend.model_dump(mode="json", exclude_none=True),
        "profile": profile.model_dump(mode="json"),
        "prepared_source_revision": source,
    }
    return sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


_BINDING_REF = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_APP_RUN_STATUSES = frozenset(
    ("starting", "active", "updating", "stopped", "failed", "unknown")
)
_SOURCE_UPDATE_STAGES = frozenset(
    ("reserved", "source-applied", "context-committed", "reloaded", "unknown")
)
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def host_endpoint_fingerprint(endpoint: str) -> str:
    """Return a safe stable identity for a host endpoint without persisting credentials."""
    return sha256(_safe_text(endpoint, field="host endpoint").encode()).hexdigest()


def _validate_source_update_receipt(receipt: dict[str, JsonValue]) -> None:
    if not isinstance(receipt, dict) or set(receipt) != {
        "update_id",
        "owner_ref",
        "generation",
        "old_source_revision",
        "new_source_revision",
        "stage",
    }:
        raise ValueError("source update receipt is invalid")
    for field, value in receipt.items():
        if not isinstance(value, str):
            raise ValueError("source update receipt is invalid")
        _safe_text(value, field=f"source update {field}")
    if receipt["stage"] not in _SOURCE_UPDATE_STAGES:
        raise ValueError("source update receipt has an invalid stage")
    if (
        _SOURCE_REVISION.fullmatch(receipt["old_source_revision"]) is None
        or _SOURCE_REVISION.fullmatch(receipt["new_source_revision"]) is None
    ):
        raise ValueError("source update receipt has an invalid source revision")


@dataclass(frozen=True)
class AppRun:
    """A selected-target record, never proof that an owner is still live."""

    id: str
    task_slug: str
    repo: str
    profile: str
    profile_revision: str
    backend: str
    backend_revision: str
    host_name: str
    host_scope: Literal["user", "project"]
    host_endpoint_fingerprint: str
    safe_target_label: str
    private_binding_ref: str
    operation: str
    protocol_version: Literal[1]
    capabilities: tuple[str, ...]
    owner_ref: str | None
    owner_generation: str | None
    status: Literal["starting", "active", "updating", "stopped", "failed", "unknown"]
    revision: int
    created_at: datetime
    updated_at: datetime
    binary_provenance: dict[str, JsonValue] | None
    target_aliases: tuple[str, ...] = ()
    source_update_receipt: dict[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        for field in (
            "id",
            "task_slug",
            "repo",
            "profile",
            "profile_revision",
            "backend",
            "backend_revision",
            "host_name",
            "safe_target_label",
            "operation",
        ):
            _safe_text(getattr(self, field), field=field.replace("_", " "))
        if self.host_scope not in {"user", "project"}:
            raise ValueError("host scope must be user or project")
        if not _FINGERPRINT.fullmatch(self.host_endpoint_fingerprint):
            raise ValueError("host endpoint fingerprint must be a SHA-256 hex digest")
        if not _BINDING_REF.fullmatch(self.private_binding_ref):
            raise ValueError(
                "private binding reference must be an opaque generated key"
            )
        if self.protocol_version != 1:
            raise ValueError("app run protocol version must be 1")
        if self.status not in _APP_RUN_STATUSES:
            raise ValueError("invalid app run status")
        if self.revision < 0:
            raise ValueError("app run revision must be non-negative")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("app run timestamps must be timezone-aware")
        if not all(isinstance(value, str) and value for value in self.capabilities):
            raise ValueError("app run capabilities must contain non-empty strings")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("app run capabilities must not contain duplicates")
        if not all(isinstance(alias, str) and alias for alias in self.target_aliases):
            raise ValueError("app run target aliases must contain non-empty strings")
        if len(set(self.target_aliases)) != len(self.target_aliases):
            raise ValueError("app run target aliases must not contain duplicates")
        for alias in self.target_aliases:
            _safe_text(alias, field="app run target alias")
        if (self.owner_ref is None) != (self.owner_generation is None):
            raise ValueError(
                "app run owner reference and generation must be supplied together"
            )
        if self.status in {"active", "updating"} and self.owner_ref is None:
            raise ValueError(
                "active or updating app runs require an owner reference and generation"
            )
        if self.owner_ref is not None:
            _safe_text(self.owner_ref, field="owner reference")
            _safe_text(self.owner_generation, field="owner generation")
        if self.binary_provenance is not None:
            raise ValueError(
                "binary provenance is unavailable until a trusted build identity contract exists"
            )
        if self.source_update_receipt is not None:
            _validate_source_update_receipt(self.source_update_receipt)

    def public_projection(self) -> dict[str, object]:
        """Return safe selected metadata without a binding or raw provenance."""
        return {
            "id": self.id,
            "task_slug": self.task_slug,
            "repo": self.repo,
            "profile": self.profile,
            "profile_revision": self.profile_revision,
            "backend": self.backend,
            "backend_revision": self.backend_revision,
            "host_name": self.host_name,
            "host_scope": self.host_scope,
            "host_endpoint_fingerprint": self.host_endpoint_fingerprint,
            "safe_target_label": self.safe_target_label,
            "target_aliases": self.target_aliases,
            "operation": self.operation,
            "protocol_version": self.protocol_version,
            "capabilities": self.capabilities,
            "status": self.status,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "binary_provenance": None,
        }
