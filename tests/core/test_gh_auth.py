import pytest

from mship.core.gh_auth import resolve_token, git_cred_args


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
