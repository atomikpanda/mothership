from pathlib import Path

from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.state import StateManager
from mship.util.shell import ShellResult


def _app(tmp_path: Path, auth_token: str | None = None):
    state = StateManager(tmp_path / ".mothership")
    return create_app(
        specs_dir=tmp_path / "specs",
        state_manager=state,
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
        auth_token=auth_token,
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
