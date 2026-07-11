"""Broker B: the standalone relay gh-token broker app (bearer-auth'd GET
/gh-token minting GitHub App installation tokens). `mint_installation_token`
itself is exercised in tests/core/test_gh_app.py — here we only check the
FastAPI wiring: auth, config validation, and error surfacing."""
import pytest
from fastapi.testclient import TestClient

from mship.core.gh_app import GhAppError
from mship.core.relay.gh_broker_app import create_gh_broker_app

_CFG = dict(bearer_token="secret", app_id="123", private_key="-----BEGIN FAKE KEY-----", installation_id="456")


def _client(monkeypatch, mint=None, **overrides):
    if mint is not None:
        monkeypatch.setattr("mship.core.relay.gh_broker_app.mint_installation_token", mint)
    cfg = {**_CFG, **overrides}
    return TestClient(create_gh_broker_app(**cfg))


def test_gh_token_success_with_valid_bearer(monkeypatch):
    captured = {}

    def fake_mint(*, app_id, private_key, installation_id, repos):
        captured.update(app_id=app_id, private_key=private_key,
                        installation_id=installation_id, repos=repos)
        return {"token": "ghs_mocktoken", "expires_at": "2026-07-11T01:00:00Z",
                "repositories": list(repos)}

    client = _client(monkeypatch, mint=fake_mint)
    r = client.get("/gh-token", params={"repos": "r1,r2"},
                   headers={"Authorization": "Bearer secret"})

    assert r.status_code == 200
    assert r.json() == {
        "token": "ghs_mocktoken",
        "expires_at": "2026-07-11T01:00:00Z",
        "repositories": ["r1", "r2"],
    }
    # the broker's own App config was threaded through to mint_installation_token
    assert captured["app_id"] == "123"
    assert captured["private_key"] == "-----BEGIN FAKE KEY-----"
    assert captured["installation_id"] == "456"
    assert captured["repos"] == ["r1", "r2"]


def test_gh_token_missing_bearer_is_401(monkeypatch):
    client = _client(monkeypatch, mint=lambda **kw: {"token": "t", "expires_at": None, "repositories": []})
    r = client.get("/gh-token", params={"repos": "r1"})
    assert r.status_code == 401


def test_gh_token_blank_bearer_is_401(monkeypatch):
    client = _client(monkeypatch, mint=lambda **kw: {"token": "t", "expires_at": None, "repositories": []})
    r = client.get("/gh-token", params={"repos": "r1"}, headers={"Authorization": ""})
    assert r.status_code == 401


def test_gh_token_wrong_bearer_is_401(monkeypatch):
    client = _client(monkeypatch, mint=lambda **kw: {"token": "t", "expires_at": None, "repositories": []})
    r = client.get("/gh-token", params={"repos": "r1"}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_gh_token_missing_app_config_is_clear_error_not_a_crash(monkeypatch):
    # No app_id / private_key / installation_id configured (e.g. env not set
    # when the CLI built the app) — must fail cleanly per-request, not 200,
    # not an unhandled exception, and must not echo the (absent) key material.
    client = _client(monkeypatch, app_id=None, private_key=None, installation_id=None)
    r = client.get("/gh-token", params={"repos": "r1"}, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "-----BEGIN" not in detail
    assert "secret" not in detail  # bearer token itself never leaks either


def test_gh_token_missing_repos_is_400_and_does_not_call_mint(monkeypatch):
    def fake_mint(**kw):
        pytest.fail("mint_installation_token must not be called when repos is missing")

    client = _client(monkeypatch, mint=fake_mint)
    r = client.get("/gh-token", headers={"Authorization": "Bearer secret"})

    assert r.status_code == 400
    assert "repos" in r.json()["detail"]


def test_gh_token_blank_repos_is_400_and_does_not_call_mint(monkeypatch):
    def fake_mint(**kw):
        pytest.fail("mint_installation_token must not be called when repos is blank")

    client = _client(monkeypatch, mint=fake_mint)
    r = client.get("/gh-token", params={"repos": ""}, headers={"Authorization": "Bearer secret"})

    assert r.status_code == 400
    assert "repos" in r.json()["detail"]


def test_gh_token_mint_failure_surfaces_repo_not_a_200(monkeypatch):
    def fake_mint(**kw):
        raise GhAppError("gh-app: installation-token mint failed (422) for repos ['nope'] — check the App is installed")

    client = _client(monkeypatch, mint=fake_mint)
    r = client.get("/gh-token", params={"repos": "nope"}, headers={"Authorization": "Bearer secret"})

    assert r.status_code == 502
    assert "nope" in r.json()["detail"]


def test_gh_token_never_logs_the_private_key_or_token(monkeypatch, caplog):
    def fake_mint(*, app_id, private_key, installation_id, repos):
        return {"token": "ghs_supersecret", "expires_at": None, "repositories": list(repos)}

    client = _client(monkeypatch, mint=fake_mint)
    with caplog.at_level("INFO"):
        client.get("/gh-token", params={"repos": "r1"}, headers={"Authorization": "Bearer secret"})

    text = caplog.text
    assert "ghs_supersecret" not in text
    assert "-----BEGIN FAKE KEY-----" not in text
