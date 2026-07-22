from pathlib import Path
import typer
from typer.testing import CliRunner

from mship.cli import relay as relay_cli


def _app():
    app = typer.Typer()
    relay_cli.register(app, get_container=lambda: None)
    return app


def test_egress_server_builds_provider_and_hands_off_to_uvicorn(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(relay_cli, "_run_uvicorn",
                        lambda app, host, port: captured.update(app=app, host=host, port=port))
    monkeypatch.setenv("MSHIP_GH_APP_ID", "123")
    key = tmp_path / "app.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    monkeypatch.setenv("MSHIP_GH_APP_KEY", str(key))
    result = CliRunner().invoke(_app(), [
        "relay", "egress-server",
        "--grant-store-dir", str(tmp_path / "grants-store"),
        "--run-token-dir", str(tmp_path / "run-tokens-store"),
        "--port", "47280",
    ])
    assert result.exit_code == 0, result.output
    assert captured["port"] == 47280 and captured["app"] is not None


def test_egress_server_refuses_unreadable_key(tmp_path, monkeypatch):
    monkeypatch.setattr(relay_cli, "_run_uvicorn", lambda *a, **k: None)
    monkeypatch.setenv("MSHIP_GH_APP_ID", "123")
    monkeypatch.setenv("MSHIP_GH_APP_KEY", str(tmp_path / "does-not-exist.pem"))
    result = CliRunner().invoke(_app(), [
        "relay", "egress-server",
        "--grant-store-dir", str(tmp_path / "grants-store"),
        "--run-token-dir", str(tmp_path / "run-tokens-store"),
    ])
    assert result.exit_code != 0
