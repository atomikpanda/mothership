"""`mship run-host` CLI: add/list/remove the gitignored role->connection store
(`RunHostStore`, `.mothership/run-hosts.yaml`). See `mship.core.run_host.store`.
"""
from pathlib import Path


import pytest
import yaml
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.relay.pairing import build_pair_link
from mship.core.run_host.config import HostRegistration, RunHostConnection
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


def _host(name: str, url: str, token: str) -> HostRegistration:
    return HostRegistration(name, (name,), (), 0, RunHostConnection(url, token), "project")


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
        assert raw["version"] == 1
        assert raw["hosts"]["ios-sim-host"]["connection"] == {
            "url": "http://10.0.0.5:8787", "token": "secret-tok",
        }
    finally:
        _reset()


def test_add_via_pair_link_parses_url_and_token(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        link = build_pair_link(url="http://10.0.0.9:9999", token="linked-tok", workspace="w")
        result = runner.invoke(app, ["run-host", "add", "android-emu-host", "--pair-link", link])
        assert result.exit_code == 0, result.output

        store = RunHostStore(ws / ".mothership")
        assert store.connection_for_role("android-emu-host", environ={}) == RunHostConnection(
            url="http://10.0.0.9:9999", token="linked-tok"
        )
    finally:
        _reset()


def test_add_with_neither_url_token_nor_pair_link_errors(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        result = runner.invoke(app, ["run-host", "add", "role-x"])
        assert result.exit_code != 0
        assert "role-x" not in RunHostStore(ws / ".mothership").effective_hosts()
    finally:
        _reset()


def test_add_with_only_url_and_no_token_errors(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        result = runner.invoke(app, ["run-host", "add", "role-x", "--url", "http://h"])
        assert result.exit_code != 0
        assert "role-x" not in RunHostStore(ws / ".mothership").effective_hosts()
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
        assert "role-x" not in RunHostStore(ws / ".mothership").effective_hosts()
    finally:
        _reset()


def test_list_redacts_token(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    try:
        store = RunHostStore(ws / ".mothership")
        store.set_host(_host("ios-sim-host", "http://h1", "super-secret-token"), scope="project")
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
        store.set_host(HostRegistration("ios-sim-host", ("ios-sim-host",), (), 0, RunHostConnection(url="http://h", token="t"), "project"), scope="project")

        result = runner.invoke(app, ["run-host", "remove", "ios-sim-host"])
        assert result.exit_code == 0, result.output
        assert "ios-sim-host" not in store.effective_hosts()
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


def test_migrate_previews_then_applies_valid_registry(tmp_path):
    ws = _ws(tmp_path)
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrun_hosts: [ios]\nrepos: {}\n"
    )
    _configure(ws)
    registry = ws / ".mothership" / "run-hosts.yaml"
    original = b"ios:\n  url: https://host.invalid\n  token: private-token\n"
    registry.write_bytes(original)
    try:
        preview = runner.invoke(app, ["run-host", "migrate"])
        assert preview.exit_code == 0, preview.output
        assert registry.read_bytes() == original

        applied = runner.invoke(app, ["run-host", "migrate", "--apply"])
        assert applied.exit_code == 0, applied.output
        assert "private-token" not in preview.output + applied.output
        assert RunHostStore(ws / ".mothership").connection_for_role(
            "ios", environ={}
        ) == RunHostConnection("https://host.invalid", "private-token")
    finally:
        _reset()


def test_migrate_malformed_registry_never_echoes_credential_text(tmp_path):
    ws = _ws(tmp_path)
    _configure(ws)
    secret = "private-token-must-not-appear"
    (ws / ".mothership" / "run-hosts.yaml").write_text(
        f"ios:\n  url: https://host.invalid\n  token: {secret}:\n"
    )
    try:
        result = runner.invoke(app, ["run-host", "migrate", "--scope", "project"])
        assert result.exit_code == 1
        assert secret not in result.output
        assert "could not read private run-host registry" in result.output
    finally:
        _reset()


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        (["run-host", "list"], []),
        (["run-host", "remove", "studio"], []),
        (["run-host", "allow-role", "ios", "--all"], []),
        (["run-host", "add", "studio", "--url", "https://host.invalid", "--token", "fresh-token"], []),
    ],
)
def test_registry_commands_redact_malformed_private_registry(tmp_path, command, extra):
    ws = _ws(tmp_path)
    _configure(ws)
    secret = "private-token-must-not-appear"
    (ws / ".mothership" / "run-hosts.yaml").write_text(
        f"ios:\n  url: https://host.invalid\n  token: {secret}:\n"
    )
    try:
        result = runner.invoke(app, [*command, *extra])
        assert result.exit_code == 1
        assert secret not in result.output
        assert "could not read private run-host registry" in result.output
    finally:
        _reset()
