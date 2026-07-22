from __future__ import annotations

from dataclasses import dataclass

from mship.core.relay.egress.enforce import Enforcer, GitSmartHttpEnforcer
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
    """v1 ships ONLY the git path (github.com), which is fully branch-enforced.

    api.github.com is intentionally NOT routed yet: a repo-scoped App token can
    mutate refs/contents via the REST API, which would bypass the git push-to-run-
    branch enforcement (the HostLockedEnforcer applies no ref policy). No worker uses
    the API leg in v1 (its client wiring is a later slice), so an under-enforced API
    route must not be reachable. The api.github.com route returns when that slice adds
    a real API enforcer (method/permission-scoped). Adding a host stays a data entry."""
    return RouteTable(
        {
            "github.com": Route(provider=provider, enforcer=GitSmartHttpEnforcer()),
        }
    )
