"""Strict data contracts for profile-driven target discovery."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mship.core.capture import Artifact
from mship.core.run_host.config import HostRegistration

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]

_MAX_JSON_DEPTH = 16
_MAX_JSON_ITEMS = 512
_MAX_JSON_STRING = 16 * 1024


def _json_value(value: object, *, depth: int = 0, items: list[int] | None = None) -> JsonValue:
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
                raise ValueError("JSON-compatible mappings require non-empty string keys")
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
    discover_task: str
    operations: dict[str, str]

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

    @field_validator("backend", "backend_revision", "profile", "profile_revision", "task", "repo", "operation")
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
    task: str
    repo: str
    profile: str
    backend: str
    logical_task: str
    operation: str
    request: dict[str, JsonValue]
    run_id: str | None
    preparation: Literal["discover", "launch", "observe"]


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
        self.details = tuple(_safe_text(detail, field="selection detail") for detail in details)
        super().__init__(self.message)


def profile_revision(profile: RunProfile, backend: BackendConfig, *, prepared_source_revision: str) -> str:
    """Fingerprint the effective definition and immutable source snapshot."""
    source = _safe_text(prepared_source_revision, field="prepared source revision")
    payload = {
        "backend": backend.model_dump(mode="json"),
        "profile": profile.model_dump(mode="json"),
        "prepared_source_revision": source,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
