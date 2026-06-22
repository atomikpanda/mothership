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
