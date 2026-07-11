import json

import httpx
import pytest

from mship.core.gh_auth import (
    broker_config_from_env,
    create_pr_via_httpx,
    get_default_branch_via_httpx,
    git_cred_args,
    resolve_token,
)


def test_resolve_token_precedence(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh_env")
    monkeypatch.setenv("GITHUB_TOKEN", "github_env")
    assert resolve_token("explicit") == "explicit"          # flag wins
    assert resolve_token(None) == "gh_env"                   # GH_TOKEN next
    monkeypatch.delenv("GH_TOKEN")
    assert resolve_token(None) == "github_env"               # GITHUB_TOKEN last
    monkeypatch.delenv("GITHUB_TOKEN")
    assert resolve_token(None) is None                       # none


def test_resolve_token_blank_is_none(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert resolve_token("   ") is None                      # blank flag ignored


def test_resolve_token_no_broker_configured_is_backward_compatible(monkeypatch):
    # No broker_url passed (the zero-arg-broker call shape) -> identical to the
    # pre-broker behavior even with a client available, since the broker leg
    # must never trigger without a broker_url.
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def handler(request):
        raise AssertionError("broker must not be called when broker_url is None")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert resolve_token(None, client=client) is None
    assert resolve_token("explicit", client=client) == "explicit"


def test_resolve_token_broker_pull_returns_token(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "token": "brokered-tok", "expires_at": "2026-01-01T00:00:00Z",
            "repositories": ["mothership", "ground-control"],
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    token = resolve_token(
        None,
        broker_url="http://127.0.0.1:9999",
        broker_bearer="serve-secret",
        repos=["mothership", "ground-control"],
        client=client,
    )
    assert token == "brokered-tok"
    assert captured["url"].startswith("http://127.0.0.1:9999/gh-token")
    assert "mothership" in captured["url"]
    assert "ground-control" in captured["url"]
    assert captured["auth"] == "Bearer serve-secret"


def test_resolve_token_explicit_wins_over_broker(monkeypatch):
    def handler(request):
        raise AssertionError("broker must not be called when an explicit token is given")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    token = resolve_token(
        "explicit-tok", broker_url="http://broker", broker_bearer="b",
        repos=["r"], client=client,
    )
    assert token == "explicit-tok"


def test_resolve_token_gh_token_env_wins_over_broker(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh_env")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def handler(request):
        raise AssertionError("broker must not be called when GH_TOKEN is set")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    token = resolve_token(
        None, broker_url="http://broker", broker_bearer="b", client=client,
    )
    assert token == "gh_env"


def test_resolve_token_broker_5xx_returns_none(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def handler(request):
        return httpx.Response(500, text="internal error")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert resolve_token(
        None, broker_url="http://broker", broker_bearer="b", client=client,
    ) is None


def test_resolve_token_broker_timeout_returns_none(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def handler(request):
        raise httpx.TimeoutException("timed out")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert resolve_token(
        None, broker_url="http://broker", broker_bearer="b", client=client,
    ) is None


def test_resolve_token_broker_connect_error_returns_none(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def handler(request):
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert resolve_token(
        None, broker_url="http://broker", broker_bearer="b", client=client,
    ) is None


def test_resolve_token_broker_malformed_body_returns_none(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def handler(request):
        return httpx.Response(200, json={"expires_at": "x"})  # no "token" key

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert resolve_token(
        None, broker_url="http://broker", broker_bearer="b", client=client,
    ) is None


def test_broker_config_from_env_reads_expected_keys(monkeypatch):
    monkeypatch.setenv("MSHIP_GH_BROKER_URL", "http://broker.example")
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "the-bearer")
    assert broker_config_from_env() == ("http://broker.example", "the-bearer")


def test_broker_config_from_env_defaults_to_none(monkeypatch):
    monkeypatch.delenv("MSHIP_GH_BROKER_URL", raising=False)
    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)
    assert broker_config_from_env() == (None, None)


def test_git_cred_args_token_only_in_env_not_args():
    args, env = git_cred_args("secret-tok")
    assert "secret-tok" not in " ".join(args)
    assert env["MSHIP_GH_TOKEN"] == "secret-tok"
    assert args[0] == "-c"
    assert args[1].startswith('credential.https://github.com.helper=')
    assert "$MSHIP_GH_TOKEN" in args[1]


def test_create_pr_via_httpx_posts_and_returns_html_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    url = create_pr_via_httpx(
        "tok", "o", "r", head="feat/x", base="main",
        title="T", body="B", client=client,
    )
    assert url == "https://github.com/o/r/pull/7"
    assert captured["url"] == "https://api.github.com/repos/o/r/pulls"
    assert captured["auth"] == "Bearer tok"
    assert captured["body"] == {"title": "T", "head": "feat/x", "base": "main", "body": "B"}


def test_create_pr_via_httpx_raises_on_error_status():
    def handler(request):
        return httpx.Response(422, json={"message": "Validation Failed"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        create_pr_via_httpx("tok", "o", "r", head="h", base="main",
                            title="T", body="B", client=client)


def test_create_pr_via_httpx_raises_when_no_html_url():
    def handler(request):
        return httpx.Response(201, json={"number": 7})  # 2xx but no html_url
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        create_pr_via_httpx("tok", "o", "r", head="h", base="main",
                            title="T", body="B", client=client)


def test_create_pr_via_httpx_recovers_existing_on_422_already_exists():
    # Idempotency: GitHub returns 422 when a PR already exists for the branch;
    # the httpx path must recover the existing PR URL (mirrors the gh path).
    def handler(request):
        if request.method == "POST":
            return httpx.Response(422, json={
                "message": "Validation Failed",
                "errors": [{"message": "A pull request already exists for o:feat/x."}],
            })
        assert request.method == "GET"  # lookup of the existing open PR
        return httpx.Response(200, json=[{"html_url": "https://github.com/o/r/pull/5"}])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    url = create_pr_via_httpx("tok", "o", "r", head="feat/x", base="main",
                              title="T", body="B", client=client)
    assert url == "https://github.com/o/r/pull/5"


def test_create_pr_via_httpx_422_non_existing_still_raises():
    def handler(request):
        return httpx.Response(422, json={
            "message": "Validation Failed",
            "errors": [{"message": "base is invalid"}],
        })
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        create_pr_via_httpx("tok", "o", "r", head="h", base="bad",
                            title="T", body="B", client=client)


def test_create_pr_via_httpx_network_error_becomes_runtimeerror():
    # Network failures (httpx.HTTPError) must surface as RuntimeError so the
    # finish caller's `except RuntimeError` handles them, not as a traceback.
    def handler(request):
        raise httpx.ConnectError("connection refused")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        create_pr_via_httpx("tok", "o", "r", head="h", base="main",
                            title="T", body="B", client=client)


def test_get_default_branch_via_httpx_network_error_becomes_runtimeerror():
    def handler(request):
        raise httpx.ConnectError("connection refused")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        get_default_branch_via_httpx("tok", "o", "r", client=client)


def test_get_default_branch_via_httpx():
    def handler(request):
        assert str(request.url) == "https://api.github.com/repos/o/r"
        assert request.headers.get("authorization") == "Bearer tok"
        return httpx.Response(200, json={"default_branch": "trunk"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_default_branch_via_httpx("tok", "o", "r", client=client) == "trunk"
