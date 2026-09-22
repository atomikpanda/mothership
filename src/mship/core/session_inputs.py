"""Strict identities and server-private capabilities for supervised app owners."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class SessionError(RuntimeError):
    """An actionable code without private adapter output in its message."""

    def __init__(self, code: str, message: str = "Session operation unavailable"):
        if not _OPERATION.fullmatch(code):
            raise ValueError("invalid session error code")
        self.code = code
        super().__init__(message)


def strict_object(value: object, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise SessionError("invalid", "Invalid session input")
    return value


def identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SessionError("invalid", "Invalid session identity")
    return value


def source_revision(value: object) -> str:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise SessionError("invalid", "Invalid session source identity")
    return value


def operation_name(value: object) -> str:
    if not isinstance(value, str) or not _OPERATION.fullmatch(value):
        raise SessionError("invalid", "Invalid session operation")
    return value


def private_path(value: object) -> Path:
    if not isinstance(value, str) or "\x00" in value:
        raise SessionError("invalid", "Invalid private session capability")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SessionError("invalid", "Invalid private session capability")
    return path


@dataclass(frozen=True)
class InstallFromResult:
    result_id: str
    artifact_id: str
    sha256: str

    def __post_init__(self) -> None:
        identifier(self.result_id)
        identifier(self.artifact_id)
        if not isinstance(self.sha256, str) or not _DIGEST.fullmatch(self.sha256):
            raise SessionError("invalid", "Invalid installation digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> InstallFromResult:
        data = strict_object(value, {"result_id", "artifact_id", "sha256"})
        return cls(
            identifier(data["result_id"]),
            identifier(data["artifact_id"]),
            str(data["sha256"]),
        )


@dataclass(frozen=True)
class InstallGrant:
    result: InstallFromResult
    path: Path = field(repr=False)
    size: int

    def __post_init__(self) -> None:
        if not isinstance(self.result, InstallFromResult) or not isinstance(
            self.path, Path
        ):
            raise SessionError("invalid", "Invalid installation capability")
        private_path(str(self.path))
        if type(self.size) is not int or not 0 < self.size <= 512 * 1024 * 1024:
            raise SessionError("invalid", "Invalid installation size")

    def to_private_dict(self) -> dict[str, object]:
        return {
            "result": self.result.to_dict(),
            "path": str(self.path),
            "size": self.size,
        }

    @classmethod
    def from_private_dict(cls, value: object) -> InstallGrant:
        data = strict_object(value, {"result", "path", "size"})
        if type(data["size"]) is not int:
            raise SessionError("invalid", "Invalid installation size")
        return cls(
            InstallFromResult.from_dict(data["result"]),
            private_path(data["path"]),
            data["size"],
        )


@dataclass(frozen=True)
class CaptureGrant:
    directory: Path = field(repr=False)
    kinds: tuple[str, ...]
    platform: str

    def __post_init__(self) -> None:
        if not isinstance(self.directory, Path):
            raise SessionError("invalid", "Invalid capture capability")
        private_path(str(self.directory))
        if (
            not isinstance(self.kinds, tuple)
            or not self.kinds
            or any(kind not in {"image", "layout"} for kind in self.kinds)
            or len(set(self.kinds)) != len(self.kinds)
        ):
            raise SessionError("invalid", "Invalid capture kinds")
        if self.platform not in {"android", "ios"}:
            raise SessionError("unavailable", "Session platform cannot be captured")

    def to_private_dict(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "kinds": list(self.kinds),
            "platform": self.platform,
        }

    @classmethod
    def from_private_dict(cls, value: object) -> CaptureGrant:
        data = strict_object(value, {"directory", "kinds", "platform"})
        if (
            not isinstance(data["kinds"], list)
            or not all(isinstance(item, str) for item in data["kinds"])
            or not isinstance(data["platform"], str)
        ):
            raise SessionError("invalid", "Invalid capture capability")
        return cls(
            private_path(data["directory"]), tuple(data["kinds"]), data["platform"]
        )


@dataclass(frozen=True)
class OwnerRequest:
    operation_ref: str
    operation: str
    source_revision: str
    expires_at: float
    install: InstallGrant | None = None
    capture: CaptureGrant | None = None

    def __post_init__(self) -> None:
        identifier(self.operation_ref)
        operation_name(self.operation)
        source_revision(self.source_revision)
        if (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (float, int))
            or not math.isfinite(self.expires_at)
            or self.expires_at <= 0
        ):
            raise SessionError("invalid", "Invalid session authorization lifetime")
        if self.install is not None and not isinstance(self.install, InstallGrant):
            raise SessionError("invalid", "Invalid installation capability")
        if self.capture is not None and not isinstance(self.capture, CaptureGrant):
            raise SessionError("invalid", "Invalid capture capability")
        if self.install is not None and self.operation not in {"run", "install"}:
            raise SessionError(
                "invalid", "Installation capability does not match operation"
            )
        if self.capture is not None and self.operation != "capture":
            raise SessionError("invalid", "Capture capability does not match operation")

    def to_private_dict(self) -> dict[str, object]:
        return {
            "operation_ref": self.operation_ref,
            "operation": self.operation,
            "source_revision": self.source_revision,
            "expires_at": self.expires_at,
            "install": None if self.install is None else self.install.to_private_dict(),
            "capture": None if self.capture is None else self.capture.to_private_dict(),
        }

    @classmethod
    def from_private_dict(cls, value: object) -> OwnerRequest:
        data = strict_object(
            value,
            {
                "operation_ref",
                "operation",
                "source_revision",
                "expires_at",
                "install",
                "capture",
            },
        )
        expires = data["expires_at"]
        if isinstance(expires, bool) or not isinstance(expires, (float, int)):
            raise SessionError("invalid", "Invalid session authorization lifetime")
        return cls(
            operation_ref=identifier(data["operation_ref"]),
            operation=operation_name(data["operation"]),
            source_revision=source_revision(data["source_revision"]),
            expires_at=float(expires),
            install=None
            if data["install"] is None
            else InstallGrant.from_private_dict(data["install"]),
            capture=None
            if data["capture"] is None
            else CaptureGrant.from_private_dict(data["capture"]),
        )
