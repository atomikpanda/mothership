# tests/core/run_host/test_store.py
"""RunHostStore: the gitignored `.mothership/run-hosts.yaml` role->connection
map. `state_dir` here is the `.mothership` dir itself (mirrors StateManager /
InboxLease — see src/mship/core/state.py), so the store file lives directly
at `state_dir / "run-hosts.yaml"`.
"""
from pathlib import Path

import yaml

from mship.core.run_host.config import RunHostConnection
from mship.core.run_host.store import RunHostStore


def test_get_missing_role_returns_none(tmp_path: Path):
    store = RunHostStore(tmp_path)
    assert store.get("ios-sim-host") is None


def test_set_then_get_roundtrips(tmp_path: Path):
    store = RunHostStore(tmp_path)
    store.set("ios-sim-host", RunHostConnection(url="http://10.0.0.5:8787", token="secret-tok"))
    conn = store.get("ios-sim-host")
    assert conn == RunHostConnection(url="http://10.0.0.5:8787", token="secret-tok")


def test_fresh_save_creates_file_with_0600_perms(tmp_path: Path):
    store = RunHostStore(tmp_path)
    path = tmp_path / "run-hosts.yaml"
    assert not path.exists()
    store.set("ios-sim-host", RunHostConnection(url="http://h", token="t"))
    assert path.exists()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_store_file_shape_is_role_to_url_token_mapping(tmp_path: Path):
    store = RunHostStore(tmp_path)
    store.set("ios-sim-host", RunHostConnection(url="http://h", token="t"))
    raw = yaml.safe_load((tmp_path / "run-hosts.yaml").read_text())
    assert raw == {"ios-sim-host": {"url": "http://h", "token": "t"}}


def test_remove_deletes_role(tmp_path: Path):
    store = RunHostStore(tmp_path)
    store.set("ios-sim-host", RunHostConnection(url="http://h", token="t"))
    store.remove("ios-sim-host")
    assert store.get("ios-sim-host") is None


def test_remove_missing_role_is_noop(tmp_path: Path):
    store = RunHostStore(tmp_path)
    store.remove("no-such-role")  # must not raise


def test_env_override_wins_over_file(tmp_path: Path, monkeypatch):
    store = RunHostStore(tmp_path)
    store.set("ios-sim-host", RunHostConnection(url="http://file-url", token="file-token"))
    monkeypatch.setenv("MSHIP_RUN_HOST_IOS_SIM_HOST_URL", "http://env-url")
    monkeypatch.setenv("MSHIP_RUN_HOST_IOS_SIM_HOST_TOKEN", "env-token")
    conn = store.get("ios-sim-host")
    assert conn == RunHostConnection(url="http://env-url", token="env-token")


def test_env_override_without_any_file_entry(tmp_path: Path, monkeypatch):
    """Role name normalization: '-' -> '_', upper-cased. No file at all needed."""
    store = RunHostStore(tmp_path)
    monkeypatch.setenv("MSHIP_RUN_HOST_ANDROID_EMU_HOST_URL", "http://emu")
    monkeypatch.setenv("MSHIP_RUN_HOST_ANDROID_EMU_HOST_TOKEN", "emu-token")
    conn = store.get("android-emu-host")
    assert conn == RunHostConnection(url="http://emu", token="emu-token")


def test_redacted_list_returns_role_and_url_never_token(tmp_path: Path):
    store = RunHostStore(tmp_path)
    store.set("ios-sim-host", RunHostConnection(url="http://h1", token="super-secret"))
    store.set("android-emu-host", RunHostConnection(url="http://h2", token="also-secret"))
    listing = store.redacted_list()
    assert listing == [("android-emu-host", "http://h2"), ("ios-sim-host", "http://h1")]
    rendered = repr(listing)
    assert "super-secret" not in rendered
    assert "also-secret" not in rendered


def test_redacted_list_empty_when_no_roles(tmp_path: Path):
    store = RunHostStore(tmp_path)
    assert store.redacted_list() == []
