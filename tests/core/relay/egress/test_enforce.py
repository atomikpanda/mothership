import pytest
from mship.core.relay.grants import Grant, Scope
from mship.core.relay.egress.request import parse_egress_request
from mship.core.relay.egress.enforce import (
    GitSmartHttpEnforcer, HostLockedEnforcer, EnforcementError,
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
