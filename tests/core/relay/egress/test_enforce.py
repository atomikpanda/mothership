import pytest
from mship.core.relay.grants import Grant, Scope
from mship.core.relay.egress.request import parse_egress_request
from mship.core.relay.egress.enforce import (
    GitSmartHttpEnforcer, HostLockedEnforcer, GitHubApiEnforcer,
    classify_api_request, EnforcementError,
)


def pkt(payload: bytes) -> bytes:
    return b"%04x" % (len(payload) + 4) + payload


def _push_body(new_oid: str, ref: str) -> bytes:
    line = f"{'0'*40} {new_oid} {ref}\x00 report-status-v2\n".encode()
    return pkt(line) + b"0000" + b"PACKxxxx"


RUN_GRANT = Grant("github-app", Scope(repos=("acme/api", "acme/web"), push_branch="feat/x"))


def _req(path, method="POST", query="", body=b""):
    return parse_egress_request(method=method, path=path, query=query, headers={}, body=body)


def test_push_to_run_branch_is_allowed():
    body = _push_body("a" * 40, "refs/heads/feat/x")
    GitSmartHttpEnforcer().check(_req("/gh/acme/api.git/git-receive-pack", body=body), RUN_GRANT)


def test_push_to_other_branch_is_rejected():
    body = _push_body("a" * 40, "refs/heads/main")
    with pytest.raises(EnforcementError):
        GitSmartHttpEnforcer().check(_req("/gh/acme/api.git/git-receive-pack", body=body), RUN_GRANT)


def test_push_to_repo_outside_run_is_rejected():
    body = _push_body("a" * 40, "refs/heads/feat/x")
    with pytest.raises(EnforcementError):
        GitSmartHttpEnforcer().check(_req("/gh/acme/other.git/git-receive-pack", body=body), RUN_GRANT)


def test_tag_push_branch_authorizes_no_ref():
    # A run scoped to a non-branch ref (refs/tags/v1) must NOT authorize that tag
    # update — the enforcer only ever allows refs/heads/ (Greptile). Both the
    # matching tag ref and any branch are refused.
    tag_grant = Grant("github-app", Scope(repos=("acme/api",), push_branch="refs/tags/v1"))
    for ref in ("refs/tags/v1", "refs/heads/feat/x"):
        body = _push_body("a" * 40, ref)
        with pytest.raises(EnforcementError):
            GitSmartHttpEnforcer().check(_req("/gh/acme/api.git/git-receive-pack", body=body), tag_grant)


def test_delete_of_run_branch_is_rejected():
    body = _push_body("0" * 40, "refs/heads/feat/x")   # new-oid all-zero = delete
    with pytest.raises(EnforcementError):
        GitSmartHttpEnforcer().check(_req("/gh/acme/api.git/git-receive-pack", body=body), RUN_GRANT)


def test_clone_fetch_upload_pack_passes():
    e = GitSmartHttpEnforcer()
    e.check(_req("/gh/acme/api.git/info/refs", method="GET", query="service=git-upload-pack"), RUN_GRANT)
    e.check(_req("/gh/acme/api.git/git-upload-pack", method="POST", body=b"0011want.."), RUN_GRANT)


def test_receive_pack_advertisement_get_passes():
    e = GitSmartHttpEnforcer()
    e.check(_req("/gh/acme/api.git/info/refs", method="GET", query="service=git-receive-pack"), RUN_GRANT)


def test_host_locked_enforcer_passes_api_traffic():
    HostLockedEnforcer().check(_req("/api/repos/acme/api/pulls", method="GET"), RUN_GRANT)


# --- GitHubApiEnforcer classifier: default-deny permit/deny matrix ----------
# `in_scope` = the path's owner/repo is within the run's scope.repos. For the
# repo-less safe globals it is irrelevant (they are allowed regardless).
_API_PERMIT = [
    ("POST", "/repos/o/r/pulls", True),                       # OPEN a PR (the only write)
    ("GET", "/repos/o/r", True),                              # repo read (root)
    ("GET", "/repos/o/r/pulls", True),                        # repo read
    ("GET", "/repos/o/r/pulls/1", True),                      # repo read
    ("GET", "/rate_limit", False),                            # safe global (scope irrelevant)
    ("GET", "/user", False),                                  # safe global (scope irrelevant)
]

_API_DENY = [
    # Managing an EXISTING numbered PR/issue is refused — the enforcer can't prove
    # the number is the run's own PR, so permitting it would let a worker touch an
    # UNRELATED PR in an in-scope repo (Greptile "PR ownership"). Only OPEN is allowed.
    ("PATCH", "/repos/o/r/pulls/1", True),            # update an existing PR (not the run's own, provably)
    ("POST", "/repos/o/r/issues/1/comments", True),   # comment on an existing issue/PR
    ("POST", "/repos/o/r/pulls/1/reviews", True),     # review an existing PR
    ("POST", "/repos/o/r/pulls/1/requested_reviewers", True),  # alter an existing PR's reviewers
    ("PUT", "/repos/o/r/pulls/1/merge", True),        # merge a PR — DIFFERENT path from PATCH /pulls/{n}
    ("PATCH", "/repos/o/r/pulls/1/merge", True),      # merge via another method is still merge
    ("POST", "/repos/o/r/merges", True),              # merges
    ("POST", "/repos/o/r/git/refs", True),            # ref create
    ("PATCH", "/repos/o/r/git/refs/heads/x", True),   # ref update
    ("DELETE", "/repos/o/r/git/refs/heads/x", True),  # ref delete
    ("PUT", "/repos/o/r/contents/f", True),           # content write
    ("DELETE", "/repos/o/r/contents/f", True),        # content delete
    ("DELETE", "/repos/o/r", True),                   # delete repo
    ("POST", "/repos/o/r/deployments", True),         # unknown write endpoint
    ("PUT", "/repos/o/r/pulls/1", True),              # method-specific: no existing-PR write is allowed
    ("POST", "/repos/o/r/pulls", False),              # permitted shape but OUT OF SCOPE
    ("GET", "/repos/o/r", False),                     # read but OUT OF SCOPE
    ("GET", "/orgs/o", True),                         # repo-less non-global read
    ("POST", "/rate_limit", False),                   # non-GET on a safe global
]


@pytest.mark.parametrize("method, path, in_scope", _API_PERMIT)
def test_classify_api_request_permits_allowlist(method, path, in_scope):
    assert classify_api_request(method, path, in_scope) is True


@pytest.mark.parametrize("method, path, in_scope", _API_DENY)
def test_classify_api_request_denies_everything_else(method, path, in_scope):
    assert classify_api_request(method, path, in_scope) is False


def test_api_enforcer_permits_in_scope_pr_create():
    GitHubApiEnforcer().check(_req("/api/repos/acme/api/pulls", method="POST"), RUN_GRANT)


def test_api_enforcer_permits_safe_global_read():
    GitHubApiEnforcer().check(_req("/api/rate_limit", method="GET"), RUN_GRANT)


def test_api_enforcer_denies_merge():
    with pytest.raises(EnforcementError):
        GitHubApiEnforcer().check(_req("/api/repos/acme/api/pulls/1/merge", method="PUT"), RUN_GRANT)


def test_api_enforcer_denies_ref_mutation():
    with pytest.raises(EnforcementError):
        GitHubApiEnforcer().check(
            _req("/api/repos/acme/api/git/refs/heads/main", method="PATCH"), RUN_GRANT
        )


def test_api_enforcer_denies_out_of_scope_repo():
    # acme/other is NOT in RUN_GRANT.repos -> even a permitted shape is refused.
    with pytest.raises(EnforcementError):
        GitHubApiEnforcer().check(_req("/api/repos/acme/other/pulls", method="POST"), RUN_GRANT)
