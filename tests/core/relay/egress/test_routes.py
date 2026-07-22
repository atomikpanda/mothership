import pytest
from mship.core.relay.egress.routes import RouteTable, UnknownHostError, build_default_routes
from mship.core.relay.egress.enforce import GitSmartHttpEnforcer, GitHubApiEnforcer


class _FakeProvider:
    def resolve(self, identity, grant, request):  # pragma: no cover - not called here
        raise NotImplementedError


def test_default_routes_include_git_and_api_enforcers():
    # The git leg is fully branch-enforced; the api leg is default-deny (PR-only)
    # via GitHubApiEnforcer. Both share the github-app provider.
    table = build_default_routes(_FakeProvider())
    assert isinstance(table.resolve("github.com").enforcer, GitSmartHttpEnforcer)
    api_route = table.resolve("api.github.com")
    assert isinstance(api_route.enforcer, GitHubApiEnforcer)
    assert api_route.provider is table.resolve("github.com").provider


def test_unknown_host_is_rejected_not_defaulted():
    table = build_default_routes(_FakeProvider())
    with pytest.raises(UnknownHostError):
        table.resolve("evil.example.com")
