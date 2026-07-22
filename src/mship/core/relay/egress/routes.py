from __future__ import annotations

from dataclasses import dataclass

from mship.core.relay.egress.enforce import Enforcer, GitSmartHttpEnforcer, HostLockedEnforcer
from mship.core.relay.egress.provider import CredentialProvider


class UnknownHostError(Exception):
    """No route for this destination host (fail closed, never default)."""


@dataclass(frozen=True)
class Route:
    provider: CredentialProvider
    enforcer: Enforcer


class RouteTable:
    """Destination host -> {provider, enforcer} as data. No github.com
    special-case in code — adding a host is a new entry."""

    def __init__(self, routes: dict[str, Route]):
        self._routes = dict(routes)

    def resolve(self, host: str) -> Route:
        try:
            return self._routes[host]
        except KeyError:
            raise UnknownHostError(host)


def build_default_routes(provider: CredentialProvider) -> RouteTable:
    return RouteTable(
        {
            "github.com": Route(provider=provider, enforcer=GitSmartHttpEnforcer()),
            "api.github.com": Route(provider=provider, enforcer=HostLockedEnforcer()),
        }
    )
