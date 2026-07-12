from pathlib import Path

from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.state import StateManager
from mship.util.shell import ShellResult


def _app(
    tmp_path: Path,
    auth_token: str | None = None,
    gh_app_id: str | None = None,
    gh_app_key: str | None = None,
):
    state = StateManager(tmp_path / ".mothership")
    return create_app(
        specs_dir=tmp_path / "specs",
        state_manager=state,
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
        auth_token=auth_token,
        gh_app_id=gh_app_id,
        gh_app_key=gh_app_key,
    )


class _FakeShellRunner:
    """Stands in for `mship.util.shell.ShellRunner`: same `.run(command, cwd,
    env=None)` signature, canned result. Installed by monkeypatching the
    `ShellRunner` name in `mship.core.serve` (where `create_app` constructs
    its own runner instances), so it's in place before `create_app` runs."""

    def __init__(self, result: ShellResult):
        self._result = result

    def run(self, command, cwd, env=None):
        return self._result


def _patch_shell(monkeypatch, result: ShellResult):
    monkeypatch.setattr("mship.core.serve.ShellRunner", lambda: _FakeShellRunner(result))


def test_create_app_accepts_gh_app_creds(tmp_path):
    # Task 2: create_app accepts + stores the Broker B App creds. The mint
    # behavior is Task 3 — here we only assert the app builds when the creds
    # are supplied (selection is by presence of these creds).
    state = StateManager(tmp_path / ".mothership")
    app = create_app(
        specs_dir=tmp_path / "specs",
        state_manager=state,
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
        auth_token="t",
        gh_app_id="123",
        gh_app_key="-----BEGIN PRIVATE KEY-----\n...",
    )
    assert app is not None


def test_gh_token_requires_bearer(tmp_path, monkeypatch):
    _patch_shell(monkeypatch, ShellResult(returncode=0, stdout="ghs_abc123\n", stderr=""))
    client = TestClient(_app(tmp_path, auth_token="secret"))
    assert client.get("/gh-token").status_code == 401


def test_gh_token_success_strips_token(tmp_path, monkeypatch):
    _patch_shell(monkeypatch, ShellResult(returncode=0, stdout="ghs_abc123\n", stderr=""))
    client = TestClient(_app(tmp_path, auth_token="secret"))
    r = client.get("/gh-token", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"token": "ghs_abc123", "expires_at": None, "repositories": None}


def test_gh_token_echoes_repos_query(tmp_path, monkeypatch):
    _patch_shell(monkeypatch, ShellResult(returncode=0, stdout="ghs_abc123\n", stderr=""))
    client = TestClient(_app(tmp_path))
    r = client.get("/gh-token", params={"repos": "mothership,ground-control"})
    assert r.status_code == 200
    assert r.json()["repositories"] == ["mothership", "ground-control"]


def test_gh_token_nonzero_exit_is_503(tmp_path, monkeypatch):
    _patch_shell(monkeypatch, ShellResult(returncode=1, stdout="", stderr="not logged in"))
    client = TestClient(_app(tmp_path))
    r = client.get("/gh-token")
    assert r.status_code == 503
    assert "gh auth login" in r.json()["detail"]


def test_gh_token_empty_stdout_is_503(tmp_path, monkeypatch):
    # e.g. `gh` reports success but has nothing to print (unlikely, but the
    # contract cares about the stripped token being non-empty, not just returncode).
    _patch_shell(monkeypatch, ShellResult(returncode=0, stdout="   \n", stderr=""))
    client = TestClient(_app(tmp_path))
    r = client.get("/gh-token")
    assert r.status_code == 503


# --- Broker B (App-backed mint) ---------------------------------------------
# Selected ONLY by App creds being configured (gh_app_id AND gh_app_key). The
# App path calls `resolve_installation` + `mint_installation_token` in
# `mship.core.serve` — monkeypatch both by that module path (mirrors how the
# Broker-A tests monkeypatch `ShellRunner`). Repos are sent as `owner/repo`.


def test_gh_token_app_path_mints_scoped_token(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        "mship.core.serve.resolve_installation",
        lambda **kw: (calls.update(resolve=kw) or "999"),
    )
    monkeypatch.setattr(
        "mship.core.serve.mint_installation_token",
        lambda **kw: (
            calls.update(mint=kw)
            or {
                "token": "ghs_x",
                "expires_at": "2026-07-12T02:00:00Z",
                "repositories": kw["repos"],
            }
        ),
    )
    client = TestClient(_app(tmp_path, auth_token="t", gh_app_id="123", gh_app_key="KEY"))
    r = client.get(
        "/gh-token?repos=acme/widgets,acme/gadgets",
        headers={"Authorization": "Bearer t"},
    )
    assert r.status_code == 200
    assert r.json()["token"] == "ghs_x"
    assert calls["resolve"]["owner"] == "acme"  # resolved from first repo
    assert calls["mint"]["installation_id"] == "999"
    assert calls["mint"]["repos"] == ["widgets", "gadgets"]  # SHORT names for the mint


def test_gh_token_app_not_installed_is_error_no_fallback(tmp_path, monkeypatch):
    from mship.core.gh_app import GhAppError

    def _boom(**kw):
        raise GhAppError("App is not installed on acme (install the App on acme)")

    monkeypatch.setattr("mship.core.serve.resolve_installation", _boom)
    # gh auth token would "work" — but must NOT be used inside the App branch:
    _patch_shell(monkeypatch, ShellResult(returncode=0, stdout="gho_fallback\n", stderr=""))
    client = TestClient(_app(tmp_path, auth_token="t", gh_app_id="123", gh_app_key="KEY"))
    r = client.get("/gh-token?repos=acme/widgets", headers={"Authorization": "Bearer t"})
    assert r.status_code in (502, 500)
    assert "acme" in r.json()["detail"]
    assert "gho_fallback" not in r.text  # never silently swapped to Broker A


def test_gh_token_repos_spanning_two_owners_is_400(tmp_path, monkeypatch):
    monkeypatch.setattr("mship.core.serve.resolve_installation", lambda **kw: "1")
    monkeypatch.setattr(
        "mship.core.serve.mint_installation_token",
        lambda **kw: {"token": "x", "expires_at": None, "repositories": kw["repos"]},
    )
    client = TestClient(_app(tmp_path, auth_token="t", gh_app_id="123", gh_app_key="KEY"))
    r = client.get("/gh-token?repos=acme/a,other/b", headers={"Authorization": "Bearer t"})
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "single" in detail or "one account" in detail


def test_gh_token_app_path_requires_owner_repo(tmp_path, monkeypatch):
    # A bare repo name (no owner) can't resolve an installation under the App
    # broker — reject with 400 rather than guessing an owner.
    monkeypatch.setattr("mship.core.serve.resolve_installation", lambda **kw: "1")
    monkeypatch.setattr(
        "mship.core.serve.mint_installation_token",
        lambda **kw: {"token": "x", "expires_at": None, "repositories": kw["repos"]},
    )
    client = TestClient(_app(tmp_path, auth_token="t", gh_app_id="123", gh_app_key="KEY"))
    r = client.get("/gh-token?repos=widgets", headers={"Authorization": "Bearer t"})
    assert r.status_code == 400
    assert "owner/repo" in r.json()["detail"]


def test_gh_token_no_app_creds_falls_back_to_gh_auth_token(tmp_path, monkeypatch):
    _patch_shell(monkeypatch, ShellResult(returncode=0, stdout="gho_daytime\n", stderr=""))
    client = TestClient(_app(tmp_path, auth_token="t"))  # NO app creds
    r = client.get("/gh-token?repos=acme/widgets", headers={"Authorization": "Bearer t"})
    assert r.status_code == 200
    assert r.json()["token"] == "gho_daytime"
