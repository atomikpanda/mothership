import pytest
from mship.core.relay.egress.routes import RouteTable, UnknownHostError, build_default_routes
from mship.core.relay.egress.enforce import GitSmartHttpEnforcer, HostLockedEnforcer


class _FakeProvider:
    def resolve(self, identity, grant, request):  # pragma: no cover - not called here
        raise NotImplementedError


def test_default_routes_map_github_and_api_hosts():
    table = build_default_routes(_FakeProvider())
    assert isinstance(table.resolve("github.com").enforcer, GitSmartHttpEnforcer)
    assert isinstance(table.resolve("api.github.com").enforcer, HostLockedEnforcer)
    assert table.resolve("github.com").provider is table.resolve("api.github.com").provider


def test_unknown_host_is_rejected_not_defaulted():
    table = build_default_routes(_FakeProvider())
    with pytest.raises(UnknownHostError):
        table.resolve("evil.example.com")
