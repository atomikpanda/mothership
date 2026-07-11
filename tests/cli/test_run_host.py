"""`mship run-host` CLI: add/list/remove the gitignored role->connection store
(`RunHostStore`, `.mothership/run-hosts.yaml`). See `mship.core.run_host.store`.
"""
from pathlib import Path

import yaml
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.relay.pairing import build_pair_link
from mship.core.run_host.config import RunHostConnection
from mship.core.run_host.store import RunHostStore

runner = CliRunner()


def _ws(root: Path) -> Path:
    ws = root / "ws"
    ws.mkdir()
    (ws / "mothership.yaml").write_text("workspace: w\nrepos: {}\n")
    (ws / ".mothership").mkdir()
    return ws


def _configure(ws: Path):
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(ws / "mothership.yaml")
    container.state_dir.override(ws / ".mothership")


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_add_via_url_token_writes_store_with_secure_perms(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        result = runner.invoke(app, [
            "run-host", "add", "ios-sim-host",
            "--url", "http://10.0.0.5:8787", "--token", "secret-tok",
        ])
        assert result.exit_code == 0, result.output

        path = ws / ".mothership" / "run-hosts.yaml"
        assert path.exists()
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

        raw = yaml.safe_load(path.read_text())
        assert raw == {"ios-sim-host": {"url": "http://10.0.0.5:8787", "token": "secret-tok"}}
    finally:
        _reset()


def test_add_via_pair_link_parses_url_and_token(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        link = build_pair_link(url="http://10.0.0.9:9999", token="linked-tok", workspace="w")
        result = runner.invoke(app, ["run-host", "add", "android-emu-host", "--pair-link", link])
        assert result.exit_code == 0, result.output

        conn = RunHostStore(ws / ".mothership").get("android-emu-host")
        assert conn == RunHostConnection(url="http://10.0.0.9:9999", token="linked-tok")
    finally:
        _reset()


def test_add_with_neither_url_token_nor_pair_link_errors(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        result = runner.invoke(app, ["run-host", "add", "role-x"])
        assert result.exit_code != 0
        assert RunHostStore(ws / ".mothership").get("role-x") is None
    finally:
        _reset()


def test_add_with_only_url_and_no_token_errors(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        result = runner.invoke(app, ["run-host", "add", "role-x", "--url", "http://h"])
        assert result.exit_code != 0
        assert RunHostStore(ws / ".mothership").get("role-x") is None
    finally:
        _reset()


def test_add_with_both_url_token_and_pair_link_errors(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        link = build_pair_link(url="http://h", token="t", workspace="w")
        result = runner.invoke(app, [
            "run-host", "add", "role-x",
            "--url", "http://h", "--token", "t", "--pair-link", link,
        ])
        assert result.exit_code != 0
        assert RunHostStore(ws / ".mothership").get("role-x") is None
    finally:
        _reset()


def test_list_redacts_token(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        RunHostStore(ws / ".mothership").set(
            "ios-sim-host", RunHostConnection(url="http://h1", token="super-secret-token"),
        )
        result = runner.invoke(app, ["run-host", "list"])
        assert result.exit_code == 0, result.output
        assert "ios-sim-host" in result.output
        assert "http://h1" in result.output
        assert "super-secret-token" not in result.output
    finally:
        _reset()


def test_list_empty_is_not_an_error(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        result = runner.invoke(app, ["run-host", "list"])
        assert result.exit_code == 0, result.output
    finally:
        _reset()


def test_remove_deletes_role(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        store = RunHostStore(ws / ".mothership")
        store.set("ios-sim-host", RunHostConnection(url="http://h", token="t"))

        result = runner.invoke(app, ["run-host", "remove", "ios-sim-host"])
        assert result.exit_code == 0, result.output
        assert store.get("ios-sim-host") is None
    finally:
        _reset()


def test_remove_missing_role_is_not_an_error(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        result = runner.invoke(app, ["run-host", "remove", "no-such-role"])
        assert result.exit_code == 0, result.output
    finally:
        _reset()
