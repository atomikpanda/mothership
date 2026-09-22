"""Private, versioned run-host registration models."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Literal, TypeAlias

from mship.core.relay.config import canonical_relay_host
_ROUTE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_RELAY_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


@dataclass(frozen=True)
class RunHostConnection:
    """Private direct endpoint mapping. Its token is never rendered."""

    url: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ValueError("direct connection URL is required")
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("direct connection token is required")

    def __repr__(self) -> str:
        return f"RunHostConnection(url={self.url!r}, token=<redacted>)"


@dataclass(frozen=True)
class RelayRunHostIdentity:
    """Durable non-secret identity of an enrolled relay run host."""

    relay: str
    host_id: str
    workspace_id: str
    instance_id: str

    def __post_init__(self) -> None:
        relay = canonical_relay_host(self.relay)
        if (
            not relay
            or len(relay) > 253
            or any(not _RELAY_LABEL.fullmatch(label) for label in relay.split("."))
            or any(
                not isinstance(value, str) or not _ROUTE_IDENTIFIER.fullmatch(value)
                for value in (self.host_id, self.workspace_id, self.instance_id)
            )
        ):
            raise ValueError("relay, host_id, workspace_id, and instance_id must be safe route identifiers")
        object.__setattr__(self, "relay", relay)


RunHostRegistrationConnection: TypeAlias = RunHostConnection | RelayRunHostIdentity


@dataclass(frozen=True)
class ResolvedRunHostConnection:
    """Ephemeral connection for one operation; never serialize or render it."""

    url: str
    token: str = field(repr=False)
    identity: tuple[str, ...] = field(repr=False)
    expires_at: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ValueError("resolved URL is required")
        if not isinstance(self.token, str) or not self.token:
            raise ValueError("resolved token is required")
        if not self.identity or not all(isinstance(part, str) and part for part in self.identity):
            raise ValueError("resolved identity is required")

    def __repr__(self) -> str:
        return "ResolvedRunHostConnection(<ephemeral>)"


@dataclass(frozen=True)
class HostRegistration:
    """One complete named host entry from a private registry scope."""

    name: str
    roles: tuple[str, ...]
    tags: tuple[str, ...]
    preference: int
    connection: RunHostRegistrationConnection = field(repr=False)
    scope: Literal["user", "project"] = "project"

    def __repr__(self) -> str:
        return (
            f"HostRegistration(name={self.name!r}, roles={self.roles!r}, "
            f"tags={self.tags!r}, preference={self.preference!r}, scope={self.scope!r})"
        )


def registration_identity(connection: RunHostRegistrationConnection) -> tuple[str, ...]:
    """Stable, non-secret recipient identity suitable for receipts and keys."""
    if isinstance(connection, RunHostConnection):
        return ("direct", connection.url)
    return (
        "relay",
        connection.relay,
        connection.host_id,
        connection.workspace_id,
        connection.instance_id,
    )


@dataclass(frozen=True)
class MigrationReport:
    """Safe outcome of a requested registry migration."""

    scope: Literal["user", "project"]
    changed: bool
    hosts: tuple[str, ...]
    backup_path: Path | None = None
