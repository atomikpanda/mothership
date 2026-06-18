import json
import httpx
import pytest

from mship.core.gh_auth import resolve_token, git_cred_args, create_pr_via_httpx, get_default_branch_via_httpx


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


def test_get_default_branch_via_httpx():
    def handler(request):
        assert str(request.url) == "https://api.github.com/repos/o/r"
        return httpx.Response(200, json={"default_branch": "trunk"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_default_branch_via_httpx("tok", "o", "r", client=client) == "trunk"
