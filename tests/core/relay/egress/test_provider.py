import httpx
import pytest
from mship.core.relay.grants import Grant, Scope
from mship.core.relay.egress.request import parse_egress_request
from mship.core.relay.egress.provider import GitHubAppProvider, ProviderError


def _req(repo_path="/gh/acme/api.git/git-receive-pack"):
    return parse_egress_request(method="POST", path=repo_path, query="", headers={}, body=b"")


def _mock_github(mint_capture: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 42})
        if request.url.path.endswith("/access_tokens"):
            import json
            mint_capture["repositories"] = json.loads(request.content)["repositories"]
            return httpx.Response(201, json={"token": "ghs_minted", "expires_at": "2026-07-22T02:00:00Z"})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


# A throwaway RSA key so _app_jwt can sign; not a real GitHub App key.
@pytest.fixture(scope="module")
def rsa_pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def test_resolve_mints_token_scoped_to_run_repos(rsa_pem):
    captured: dict = {}
    grant = Grant("github-app", Scope(repos=("acme/api",), push_branch="feat/x"))
    provider = GitHubAppProvider(app_id="1", private_key=rsa_pem, client=_mock_github(captured))
    cred = provider.resolve(identity="enr1", grant=grant, request=_req())
    assert cred.value == "ghs_minted"
    assert cred.expires_at == "2026-07-22T02:00:00Z"
    assert captured["repositories"] == ["api"]          # short name, scoped to the grant
    assert cred.attach.header == "Authorization"


def test_resolve_refuses_repo_outside_grant(rsa_pem):
    grant = Grant("github-app", Scope(repos=("acme/web",), push_branch="feat/x"))
    provider = GitHubAppProvider(app_id="1", private_key=rsa_pem, client=_mock_github({}))
    with pytest.raises(ProviderError):
        provider.resolve(identity="enr1", grant=grant, request=_req("/gh/acme/api.git/git-receive-pack"))


def test_resolve_refuses_empty_repo_scope(rsa_pem):
    grant = Grant("github-app", Scope(repos=(), push_branch="feat/x"))
    provider = GitHubAppProvider(app_id="1", private_key=rsa_pem, client=_mock_github({}))
    with pytest.raises(ProviderError):
        provider.resolve(identity="enr1", grant=grant, request=_req())
