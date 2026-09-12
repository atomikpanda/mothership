"""Private, versioned run-host registration models."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class RunHostConnection:
    """A resolved credential-bearing connection; its token is never repr'd."""

    url: str
    token: str = field(repr=False)

    def __repr__(self) -> str:
        return f"RunHostConnection(url={self.url!r}, token=<redacted>)"


@dataclass(frozen=True)
class HostRegistration:
    """One complete named host entry from a private registry scope."""

    name: str
    roles: tuple[str, ...]
    tags: tuple[str, ...]
    preference: int
    connection: RunHostConnection = field(repr=False)
    scope: Literal["user", "project"] = "project"

    def __repr__(self) -> str:
        return (
            f"HostRegistration(name={self.name!r}, roles={self.roles!r}, "
            f"tags={self.tags!r}, preference={self.preference!r}, scope={self.scope!r})"
        )


@dataclass(frozen=True)
class MigrationReport:
    """Safe outcome of a requested legacy-registry migration."""

    scope: Literal["user", "project"]
    changed: bool
    hosts: tuple[str, ...]
    backup_path: Path | None = None
