import httpx
from fastapi.testclient import TestClient
from mship.core.relay.grants import Grant, GrantStore, Scope
from mship.core.relay.run_token import issue_run_token
from mship.core.relay.egress.credential import Credential, github_token_attachment
from mship.core.relay.egress.routes import build_default_routes
from mship.core.relay.egress.proxy import build_egress_app


def pkt(payload: bytes) -> bytes:
    return b"%04x" % (len(payload) + 4) + payload


def _push_body(ref="refs/heads/feat/x") -> bytes:
    line = f"{'0'*40} {'a'*40} {ref}\x00 report-status-v2\n".encode()
    return pkt(line) + b"0000" + b"PACKxxxx"


class _StubProvider:
    """Returns a fixed credential without touching GitHub (gh_app is tested in
    Task 6). Records that resolve() was called with the run's repo."""
    def __init__(self):
        self.calls = []

    def resolve(self, identity, grant, request):
        self.calls.append((identity, tuple(grant.scope.repos), request.repo))
        return Credential(value="ghs_minted", expires_at=None, attach=github_token_attachment())


def _capture_upstream(seen: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["authorization"] = request.headers.get("Authorization")
        seen["run_token"] = request.headers.get("Mship-Run-Token")
        return httpx.Response(200, content=b"ok-from-github")
    return httpx.Client(transport=httpx.MockTransport(handler))


def _app(tmp_path, provider, client):
    gs = GrantStore(tmp_path)
    gs.set_grant("enr1", Grant("github-app", Scope(repos=("acme/api", "acme/web"))))
    routes = build_default_routes(provider) if provider else None
    return build_egress_app(
        grant_store=gs, run_token_dir=tmp_path, routes=routes, client=client,
    ), tmp_path


def _token(tmp_path):
    return issue_run_token(tmp_path, enrollment_id="enr1",
                           scope=Scope(repos=("acme/api",), push_branch="feat/x"),
                           ttl_seconds=3600)


def test_push_to_run_branch_attaches_token_and_strips_placeholder(tmp_path):
    seen: dict = {}
    provider = _StubProvider()
    app, base = _app(tmp_path, provider, _capture_upstream(seen))
    token = _token(base)
    client = TestClient(app)
    resp = client.post(
        "/gh/acme/api.git/git-receive-pack",
        content=_push_body(),
        headers={"Mship-Run-Token": token, "Content-Type": "application/x-git-receive-pack-request"},
    )
    assert resp.status_code == 200
    assert resp.content == b"ok-from-github"
    assert seen["host"] == "github.com"
    assert seen["authorization"] == "token ghs_minted"     # real cred attached at egress
    assert seen["run_token"] is None                       # placeholder never leaves the relay
    assert provider.calls == [("enr1", ("acme/api",), "acme/api")]


def test_push_to_other_branch_is_rejected_before_egress(tmp_path):
    seen: dict = {}
    app, base = _app(tmp_path, _StubProvider(), _capture_upstream(seen))
    token = _token(base)
    resp = TestClient(app).post(
        "/gh/acme/api.git/git-receive-pack",
        content=_push_body(ref="refs/heads/main"),
        headers={"Mship-Run-Token": token},
    )
    assert resp.status_code == 403
    assert seen == {}                                       # never forwarded


def test_missing_or_invalid_run_token_is_401(tmp_path):
    app, base = _app(tmp_path, _StubProvider(), _capture_upstream({}))
    c = TestClient(app)
    assert c.post("/gh/acme/api.git/git-receive-pack", content=_push_body()).status_code == 401
    assert c.post("/gh/acme/api.git/git-receive-pack", content=_push_body(),
                  headers={"Mship-Run-Token": "bogus.secret"}).status_code == 401


def test_no_provider_fails_closed_503(tmp_path):
    app, base = _app(tmp_path, None, _capture_upstream({}))
    token = _token(base)
    resp = TestClient(app).post("/gh/acme/api.git/git-receive-pack",
                                content=_push_body(), headers={"Mship-Run-Token": token})
    assert resp.status_code == 503


def test_clone_upload_pack_passes_and_attaches(tmp_path):
    seen: dict = {}
    app, base = _app(tmp_path, _StubProvider(), _capture_upstream(seen))
    token = _token(base)
    resp = TestClient(app).get(
        "/gh/acme/api.git/info/refs?service=git-upload-pack",
        headers={"Mship-Run-Token": token},
    )
    assert resp.status_code == 200
    assert seen["authorization"] == "token ghs_minted"
