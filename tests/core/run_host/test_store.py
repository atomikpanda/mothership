"""RunHostStore's canonical versioned named-host APIs."""

import os
from pathlib import Path

import yaml

import pytest

import mship.core.run_host.store as store_module

from mship.core.run_host.config import HostRegistration, RunHostConnection
from mship.core.run_host.store import RunHostStore


def _host(name: str, url: str = "http://h", token: str = "t") -> HostRegistration:
    return HostRegistration(
        name, (name,), (), 0, RunHostConnection(url, token), "project"
    )


def test_missing_effective_hosts_is_empty(tmp_path: Path):
    assert RunHostStore(tmp_path).effective_hosts() == {}


def test_set_host_then_connection_for_role_roundtrips(tmp_path: Path):
    store = RunHostStore(tmp_path)
    store.set_host(
        _host("ios-sim-host", "http://10.0.0.5:8787", "secret-tok"), scope="project"
    )
    assert store.connection_for_role("ios-sim-host", environ={}) == RunHostConnection(
        "http://10.0.0.5:8787", "secret-tok"
    )


def test_fresh_save_creates_file_with_0600_perms(tmp_path: Path):
    store = RunHostStore(tmp_path)
    path = tmp_path / "run-hosts.yaml"
    store.set_host(_host("ios-sim-host"), scope="project")
    assert path.stat().st_mode & 0o777 == 0o600


def test_token_tmp_file_is_0600_from_the_start_no_wide_window(
    tmp_path: Path, monkeypatch
):
    store = RunHostStore(tmp_path)
    real_replace = Path.replace
    seen: dict[str, int] = {}

    def spy_replace(self, target):
        seen["tmp_mode"] = self.stat().st_mode & 0o777
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    old_umask = os.umask(0o000)
    try:
        store.set_host(_host("ios-sim-host", token="super-secret"), scope="project")
    finally:
        os.umask(old_umask)
    assert seen["tmp_mode"] == 0o600
    assert (tmp_path / "run-hosts.yaml").stat().st_mode & 0o777 == 0o600


def test_store_file_shape_is_versioned_named_host_registry(tmp_path: Path):
    store = RunHostStore(tmp_path)
    store.set_host(_host("ios-sim-host"), scope="project")
    raw = yaml.safe_load((tmp_path / "run-hosts.yaml").read_text())
    assert raw["version"] == 1
    assert raw["hosts"]["ios-sim-host"]["roles"] == ["ios-sim-host"]
    assert raw["hosts"]["ios-sim-host"]["connection"] == {
        "url": "http://h",
        "token": "t",
    }


def test_remove_host_deletes_only_its_scope_entry(tmp_path: Path):
    store = RunHostStore(tmp_path)
    store.set_host(_host("ios-sim-host"), scope="project")
    store.remove_host("ios-sim-host", scope="project")
    assert store.effective_hosts() == {}


def test_remove_missing_host_is_noop(tmp_path: Path):
    RunHostStore(tmp_path).remove_host("no-such-role", scope="project")


def test_env_override_wins_over_one_allowed_host(tmp_path: Path, monkeypatch):
    store = RunHostStore(tmp_path)
    store.set_host(
        _host("ios-sim-host", "http://file-url", "file-token"), scope="project"
    )
    monkeypatch.setenv("MSHIP_RUN_HOST_IOS_SIM_HOST_URL", "http://env-url")
    monkeypatch.setenv("MSHIP_RUN_HOST_IOS_SIM_HOST_TOKEN", "env-token")
    assert store.connection_for_role(
        "ios-sim-host", environ=os.environ
    ) == RunHostConnection("http://env-url", "env-token")


def test_env_only_registration_remains_supported(tmp_path: Path, monkeypatch):
    store = RunHostStore(tmp_path)
    monkeypatch.setenv("MSHIP_RUN_HOST_ANDROID_EMU_HOST_URL", "http://emu")
    monkeypatch.setenv("MSHIP_RUN_HOST_ANDROID_EMU_HOST_TOKEN", "emu-token")
    assert store.connection_for_role(
        "android-emu-host", environ=os.environ
    ) == RunHostConnection("http://emu", "emu-token")


def test_safe_hosts_never_include_tokens(tmp_path: Path):
    store = RunHostStore(tmp_path)
    store.set_host(_host("ios-sim-host", "http://h1", "super-secret"), scope="project")
    store.set_host(
        _host("android-emu-host", "http://h2", "also-secret"), scope="project"
    )
    safe = store.safe_hosts()
    assert sorted((name, host["url"]) for name, host in safe.items()) == [
        ("android-emu-host", "http://h2"),
        ("ios-sim-host", "http://h1"),
    ]
    assert "secret" not in repr(safe)


def test_mutation_fails_without_posix_locking_before_creating_state(
    tmp_path: Path, monkeypatch
):
    state = tmp_path / "private-state"
    monkeypatch.setattr(store_module, "fcntl", None)

    with pytest.raises(store_module.RunHostError, match="POSIX file locking"):
        RunHostStore(state).set_host(_host("studio"), scope="project")

    assert not state.exists()
