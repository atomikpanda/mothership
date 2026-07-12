"""GitHub App installation-token minting (Broker B credential) — mocked GitHub API."""
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mship.core.gh_app import GhAppError, mint_installation_token, resolve_installation


@pytest.fixture(scope="module")
def rsa_keypair():
    """Throwaway RSA keypair for signing — never a real GitHub App key."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


@pytest.fixture(scope="module")
def rsa_private_key(rsa_keypair):
    """Just the private PEM from the throwaway keypair, for signing App JWTs."""
    private_pem, _public_pem = rsa_keypair
    return private_pem


def test_mint_installation_token_posts_scoped_request_and_returns_token(rsa_keypair):
    private_pem, _public_pem = rsa_keypair
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["accept"] = request.headers.get("accept")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "token": "ghs_mocktoken123",
                "expires_at": "2026-07-11T01:00:00Z",
                "repositories": [{"name": "r1"}, {"name": "r2"}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = mint_installation_token(
        app_id="123",
        private_key=private_pem,
        installation_id="456",
        repos=["r1", "r2"],
        client=client,
    )

    assert captured["url"] == "https://api.github.com/app/installations/456/access_tokens"
    assert captured["body"] == {"repositories": ["r1", "r2"]}
    assert captured["accept"] == "application/vnd.github+json"
    assert captured["auth"].startswith("Bearer ")

    assert result == {
        "token": "ghs_mocktoken123",
        "expires_at": "2026-07-11T01:00:00Z",
        "repositories": ["r1", "r2"],
    }


def test_mint_installation_token_signs_a_short_lived_app_jwt(rsa_keypair):
    private_pem, public_pem = rsa_keypair
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            201, json={"token": "t", "expires_at": "x", "repositories": []}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    mint_installation_token(
        app_id="123", private_key=private_pem, installation_id="456",
        repos=["r1"], client=client,
    )

    token_jwt = captured["auth"].removeprefix("Bearer ")
    claims = jwt.decode(token_jwt, public_pem, algorithms=["RS256"])
    assert claims["iss"] == "123"
    assert claims["exp"] - claims["iat"] == pytest.approx(600, abs=30)  # ~10 min


def test_mint_installation_token_raises_on_repo_not_in_installation(rsa_keypair):
    private_pem, _public_pem = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"message": "Validation Failed",
                  "errors": [{"message": "'nope' is not in the installation"}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GhAppError) as exc_info:
        mint_installation_token(
            app_id="123", private_key=private_pem, installation_id="456",
            repos=["r1", "nope"], client=client,
        )

    message = str(exc_info.value)
    assert "r1" in message
    assert "nope" in message


def test_mint_installation_token_never_leaks_private_key_on_error(rsa_keypair):
    private_pem, _public_pem = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Validation Failed"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GhAppError) as exc_info:
        mint_installation_token(
            app_id="123", private_key=private_pem, installation_id="456",
            repos=["r1"], client=client,
        )

    assert private_pem not in str(exc_info.value)


def test_mint_installation_token_refuses_empty_repos_without_a_network_call(rsa_keypair):
    """An empty/omitted `repos` must never reach GitHub: an empty
    `repositories` list in the request body mints a token scoped to the
    WHOLE App installation, not nothing. Assert this fails fast — no
    `.post` call at all — by using a client whose `.post` fails the test."""
    private_pem, _public_pem = rsa_keypair

    class _ExplodingClient:
        def post(self, *args, **kwargs):
            pytest.fail("mint_installation_token must not make an HTTP call for empty repos")

    with pytest.raises(GhAppError, match="unscoped"):
        mint_installation_token(
            app_id="123", private_key=private_pem, installation_id="456",
            repos=[], client=_ExplodingClient(),
        )


def test_resolve_installation_returns_id_for_repo(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/installation"
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"id": 424242})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    inst = resolve_installation(
        app_id="123", private_key=rsa_private_key,
        owner="acme", repo="widgets", client=client,
    )
    assert inst == "424242"


def test_resolve_installation_not_installed_raises_naming_repo(rsa_private_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GhAppError) as ei:
        resolve_installation(app_id="123", private_key=rsa_private_key,
                             owner="acme", repo="widgets", client=client)
    assert "acme/widgets" in str(ei.value)
