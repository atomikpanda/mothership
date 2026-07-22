import pytest

from mship.core.relay.grants import Scope, Grant, RepoSpecError, parse_repos


def test_parse_repos_accepts_owner_repo_list():
    assert parse_repos("acme/api, acme/web") == ("acme/api", "acme/web")


def test_parse_repos_rejects_empty():
    with pytest.raises(RepoSpecError):
        parse_repos("  , ,")


def test_parse_repos_rejects_slashless():
    with pytest.raises(RepoSpecError):
        parse_repos("acme/api,api")


def test_parse_repos_rejects_multi_owner():
    with pytest.raises(RepoSpecError):
        parse_repos("org-a/api,org-b/web")


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


from pathlib import Path
from mship.core.relay.grants import GrantStore


def test_set_and_get_grant_roundtrip(tmp_path: Path):
    store = GrantStore(tmp_path)
    store.set_grant("enr1", Grant("github-app", Scope(repos=("acme/api", "acme/web"))))
    grants = store.get_grants("enr1")
    assert len(grants) == 1
    assert grants[0].provider == "github-app"
    assert set(grants[0].scope.repos) == {"acme/api", "acme/web"}


def test_set_grant_replaces_same_provider(tmp_path: Path):
    store = GrantStore(tmp_path)
    store.set_grant("enr1", Grant("github-app", Scope(repos=("acme/api",))))
    store.set_grant("enr1", Grant("github-app", Scope(repos=("acme/api", "acme/web"))))
    grants = store.get_grants("enr1")
    assert len(grants) == 1
    assert set(grants[0].scope.repos) == {"acme/api", "acme/web"}


def test_get_grants_unknown_enrollment_is_empty(tmp_path: Path):
    assert GrantStore(tmp_path).get_grants("nope") == []
