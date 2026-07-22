from __future__ import annotations

from dataclasses import dataclass

from mship.core.relay.egress.enforce import Enforcer, GitSmartHttpEnforcer, GitHubApiEnforcer
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
    """v1 ships two GitHub legs, both on the github-app provider:

    - github.com    -> GitSmartHttpEnforcer: clone/fetch + push only the run
      branch to a run-scoped repo (fully branch-enforced).
    - api.github.com -> GitHubApiEnforcer: DEFAULT-DENY REST leg permitting only
      opening/managing the run's PR + comments/reviews + repo-scoped reads. It
      refuses merge, POST /merges, git/refs mutation, and contents mutation, so
      the API leg cannot sidestep the git push-to-run-branch enforcement.

    Both hosts already ride the same egress subdomain (path-prefix /gh/ vs /api/,
    one Caddy block, one tls_ask entry) and the Attachment host-locks the token to
    [github.com, api.github.com] — adding the api leg needs no new deploy surface.
    Adding a host stays a data entry (+ one /prefix/ in request.py)."""
    return RouteTable(
        {
            "github.com": Route(provider=provider, enforcer=GitSmartHttpEnforcer()),
            "api.github.com": Route(provider=provider, enforcer=GitHubApiEnforcer()),
        }
    )
