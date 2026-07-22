import pytest
from mship.core.relay.egress.routes import RouteTable, UnknownHostError, build_default_routes
from mship.core.relay.egress.enforce import GitSmartHttpEnforcer


class _FakeProvider:
    def resolve(self, identity, grant, request):  # pragma: no cover - not called here
        raise NotImplementedError


def test_default_routes_ship_git_only_with_branch_enforcer():
    # v1 ships ONLY the fully-branch-enforced git path. api.github.com is
    # intentionally not routed yet (a repo-scoped App token could mutate refs via
    # the REST API and bypass the push-to-run-branch enforcement); it returns with a
    # real API enforcer when the worker's API-client slice lands.
    table = build_default_routes(_FakeProvider())
    assert isinstance(table.resolve("github.com").enforcer, GitSmartHttpEnforcer)
    with pytest.raises(UnknownHostError):
        table.resolve("api.github.com")


def test_unknown_host_is_rejected_not_defaulted():
    table = build_default_routes(_FakeProvider())
    with pytest.raises(UnknownHostError):
        table.resolve("evil.example.com")
