from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


@pytest.fixture
def _configured(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    yield workspace

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_serve_command_registered():
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "127.0.0.1" in result.output


def test_serve_refuses_nonloopback_without_token(_configured, monkeypatch):
    # _configured overrides config_path so get_container() skips cwd-based
    # workspace discovery (see mship.cli.get_container). Without it, serve hits
    # "No mothership.yaml found" before the token check when pytest runs from a
    # bare checkout with no workspace above cwd, masking the security assertion
    # below (MOS-188).
    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code != 0
    assert "MSHIP_SERVE_TOKEN" in result.output


def test_serve_binds_nonloopback_with_token(_configured, monkeypatch):
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "secret")
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.update(k))
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 0, result.output
    assert seen.get("host") == "0.0.0.0"


def test_serve_prints_pair_link_with_token_and_concrete_host(_configured, monkeypatch):
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "secret")
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    result = runner.invoke(app, ["serve", "--host", "192.168.1.50"])
    assert result.exit_code == 0, result.output
    assert "groundcontrol://add?" in result.output
    assert "192.168.1.50" in result.output


def test_serve_pair_link_uses_detected_ip_for_bind_all(_configured, monkeypatch):
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "secret")
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr("mship.core.serve_pair._primary_ipv4", lambda: "100.1.2.3")
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 0, result.output
    assert "http://100.1.2.3" in result.output


def test_serve_no_pair_link_without_token(_configured, monkeypatch):
    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    result = runner.invoke(app, ["serve"])  # loopback default, no token
    assert result.exit_code == 0, result.output
    assert "groundcontrol://add" not in result.output


def test_relay_serve_app_serves_exec_with_config_not_503(tmp_path, monkeypatch):
    """FIX 1 regression: the relay serve path must pass `config=config` to
    `create_app` exactly like the non-relay path — otherwise `/exec/{verb}`
    (the PRIMARY transport for `--remote`) always 503s "not bootstrapped" over
    the relay. We drive `_serve_with_relay` with the tunnel machinery stubbed,
    capture the app it hands to uvicorn, and prove `/exec` is live (config
    present) rather than 503-ing."""
    from fastapi.testclient import TestClient

    from mship.cli.output import Output
    from mship.cli.serve import _serve_with_relay
    from mship.core.config import RepoConfig, WorkspaceConfig
    from mship.core.state import StateManager

    monkeypatch.setenv("MSHIP_PR_WATCH_INTERVAL", "0")  # no gh-shelling watcher loop

    repo_dir = tmp_path / "api"
    repo_dir.mkdir()
    (tmp_path / ".mothership").mkdir()
    config = WorkspaceConfig(
        workspace="t",
        repos={"api": RepoConfig(path=repo_dir, type="service", tasks={"run": "start"})},
    )

    class _FakeContainer:
        def __init__(self, state_dir):
            self._sm = StateManager(state_dir)

        def state_manager(self):
            return self._sm

        def log_manager(self):
            return None

        def worktree_manager(self):
            return None

    class _FakeSup:
        restart_count = 0

        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def tick(self):
            pass

        def recent_output(self):
            return ""

    # Stub the whole tunnel/key/token/health surface so nothing touches ssh,
    # ~/.ssh, DNS, or the network — we only care about the app create_app builds.
    monkeypatch.setattr("mship.core.relay.token.ensure_serve_token", lambda root: "test-token")
    monkeypatch.setattr("mship.core.relay.keys.ensure_relay_key", lambda home=None: tmp_path / "key")
    monkeypatch.setattr("mship.core.relay.keys.relay_public_key", lambda key_path: "ssh-ed25519 AAAAfake")
    monkeypatch.setattr("mship.core.relay.tunnel.device_id", lambda pub: "dev")
    monkeypatch.setattr("mship.core.relay.tunnel.device_subdomain", lambda ws, dev: "sub")
    monkeypatch.setattr("mship.core.relay.tunnel.build_tunnel_argv", lambda *a, **k: ["true"])
    monkeypatch.setattr("mship.core.relay.tunnel.TunnelSupervisor", _FakeSup)
    monkeypatch.setattr("mship.core.relay.pairing.build_pair_link", lambda **k: "groundcontrol://add?x=1")
    monkeypatch.setattr("mship.core.relay.health.wait_until_reachable", lambda *a, **k: (True, ""))

    captured: dict = {}

    def _capture_run(api, **kw):
        captured["app"] = api

    monkeypatch.setattr("uvicorn.run", _capture_run)

    _serve_with_relay(
        container=_FakeContainer(tmp_path / ".mothership"),
        config=config,
        workspace_root=tmp_path,
        output=Output(),
        relay_host_override="relay.example.test",
        port=47199,
        relay_tick=0.01,
    )

    api = captured["app"]
    assert api is not None, "relay path never handed an app to uvicorn.run"

    client = TestClient(api)
    # Unknown repo -> config IS wired in (200 data-error), NOT a 503
    # "not bootstrapped". The 503 is exactly what a missing config would give.
    r = client.post(
        "/exec/run",
        json={"task": "t1", "repos": ["ghost-repo"]},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code != 503, "relay-created app 503'd — config was not passed to create_app"
    assert r.status_code == 200


# --- Task 2: GitHub App broker (Broker B) creds threaded into create_app ---
# create_app is imported locally (`from mship.core.serve import create_app`)
# inside the serve command, so these patch it at its SOURCE module
# (mship.core.serve) — patching mship.cli.serve.create_app would miss the
# call-time local rebind.


def test_serve_threads_gh_app_creds_into_create_app(_configured, monkeypatch, tmp_path):
    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)
    key_file = tmp_path / "app.pem"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\nKEYTEXT\n-----END PRIVATE KEY-----\n")
    monkeypatch.setenv("MSHIP_GH_APP_ID", "123")
    monkeypatch.setenv("MSHIP_GH_APP_KEY", str(key_file))

    captured: dict = {}

    def _fake_create_app(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("mship.core.serve.create_app", _fake_create_app)
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    # id passed through as-is; the KEY env var is a PATH — its TEXT is read.
    assert captured["gh_app_id"] == "123"
    assert "KEYTEXT" in captured["gh_app_key"]


def test_serve_warns_on_ignored_installation_var(_configured, monkeypatch):
    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)
    monkeypatch.setenv("MSHIP_GH_APP_INSTALLATION", "42")
    monkeypatch.setattr("mship.core.serve.create_app", lambda **k: object())
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    assert "MSHIP_GH_APP_INSTALLATION is ignored" in result.output


def test_serve_warns_and_disables_when_gh_app_key_unreadable(_configured, monkeypatch, tmp_path):
    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)
    monkeypatch.setenv("MSHIP_GH_APP_ID", "123")
    monkeypatch.setenv("MSHIP_GH_APP_KEY", str(tmp_path / "missing.pem"))

    captured: dict = {}
    monkeypatch.setattr(
        "mship.core.serve.create_app",
        lambda **k: (captured.update(k), object())[1],
    )
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    assert "App minting disabled" in result.output
    # id still passes through; the unreadable key path yields no key text.
    assert captured["gh_app_id"] == "123"
    assert captured["gh_app_key"] is None
