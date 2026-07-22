import pytest
from mship.core.relay.egress.request import parse_egress_request, UnmappablePathError


def test_gh_prefix_maps_to_github_and_extracts_repo_and_receive_service():
    req = parse_egress_request(
        method="POST",
        path="/gh/acme/api.git/git-receive-pack",
        query="",
        headers={},
        body=b"",
    )
    assert req.upstream_host == "github.com"
    assert req.upstream_path == "/acme/api.git/git-receive-pack"
    assert req.repo == "acme/api"
    assert req.service == "git-receive-pack"
    assert req.is_receive_pack_post is True


def test_info_refs_service_comes_from_query():
    req = parse_egress_request(
        method="GET",
        path="/gh/acme/api.git/info/refs",
        query="service=git-upload-pack",
        headers={},
        body=b"",
    )
    assert req.service == "git-upload-pack"
    assert req.is_receive_pack_post is False


def test_api_prefix_extracts_repo_from_repos_path():
    req = parse_egress_request(
        method="POST", path="/api/repos/acme/api/pulls", query="", headers={}, body=b"",
    )
    assert req.upstream_host == "api.github.com"
    assert req.upstream_path == "/repos/acme/api/pulls"
    assert req.repo == "acme/api"


def test_api_repo_less_global_paths_yield_none():
    for p in ("/api/user", "/api/rate_limit", "/api/graphql", "/api/orgs/acme"):
        req = parse_egress_request(method="GET", path=p, query="", headers={}, body=b"")
        assert req.upstream_host == "api.github.com"
        assert req.repo is None


def test_api_repo_extraction_tolerates_query_string():
    req = parse_egress_request(
        method="GET", path="/api/repos/acme/api/pulls", query="state=open&per_page=100",
        headers={}, body=b"",
    )
    assert req.repo == "acme/api"


def test_unmapped_prefix_raises():
    with pytest.raises(UnmappablePathError):
        parse_egress_request(method="GET", path="/evil/x", query="", headers={}, body=b"")
