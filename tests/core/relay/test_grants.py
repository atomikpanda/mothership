from mship.core.relay.grants import Scope, Grant


def test_scope_covers_is_repo_subset_ignoring_push_branch():
    ceiling = Scope(repos=("acme/api", "acme/web"))               # ceiling: push_branch None
    run = Scope(repos=("acme/api",), push_branch="feat/x")        # per-run subset
    assert ceiling.covers(run) is True


def test_scope_does_not_cover_repo_outside_ceiling():
    ceiling = Scope(repos=("acme/api",))
    run = Scope(repos=("acme/api", "acme/secret"), push_branch="feat/x")
    assert ceiling.covers(run) is False


def test_grant_carries_provider_and_scope():
    g = Grant(provider="github-app", scope=Scope(repos=("acme/api",)))
    assert g.provider == "github-app"
    assert g.scope.repos == ("acme/api",)
