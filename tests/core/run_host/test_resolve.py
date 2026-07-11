# tests/core/run_host/test_resolve.py
"""resolve_run_host precedence + actionable errors.

Precedence: explicit --remote=<role> > repo's declared run_host > the sole
`config.run_hosts` entry. Ambiguous / unknown-role / declared-but-unmapped
all raise RunHostError with an actionable message.
"""
from pathlib import Path

import pytest

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.run_host.config import RunHostConnection
from mship.core.run_host.store import RunHostError, RunHostStore, resolve_run_host


def _config(run_hosts):
    return WorkspaceConfig(workspace="test", run_hosts=run_hosts, repos={})


def _repo(run_host=None):
    return RepoConfig(path=Path("."), type="library", run_host=run_host)


def test_explicit_role_wins_over_repo_and_sole_entry(tmp_path):
    store = RunHostStore(tmp_path)
    store.set("ios-sim-host", RunHostConnection(url="http://ios", token="ios-tok"))
    store.set("android-emu-host", RunHostConnection(url="http://android", token="android-tok"))
    config = _config(["ios-sim-host", "android-emu-host"])
    repo = _repo(run_host="ios-sim-host")

    conn = resolve_run_host(
        "android-emu-host", repo=repo, config=config, store=store
    )
    assert conn == RunHostConnection(url="http://android", token="android-tok")


def test_falls_back_to_repo_run_host(tmp_path):
    store = RunHostStore(tmp_path)
    store.set("ios-sim-host", RunHostConnection(url="http://ios", token="ios-tok"))
    config = _config(["ios-sim-host", "android-emu-host"])
    repo = _repo(run_host="ios-sim-host")

    conn = resolve_run_host(None, repo=repo, config=config, store=store)
    assert conn == RunHostConnection(url="http://ios", token="ios-tok")


def test_falls_back_to_sole_config_entry_when_no_repo_role(tmp_path):
    store = RunHostStore(tmp_path)
    store.set("ios-sim-host", RunHostConnection(url="http://ios", token="ios-tok"))
    config = _config(["ios-sim-host"])
    repo = _repo(run_host=None)

    conn = resolve_run_host(None, repo=repo, config=config, store=store)
    assert conn == RunHostConnection(url="http://ios", token="ios-tok")


def test_falls_back_to_sole_config_entry_when_repo_is_none(tmp_path):
    store = RunHostStore(tmp_path)
    store.set("ios-sim-host", RunHostConnection(url="http://ios", token="ios-tok"))
    config = _config(["ios-sim-host"])

    conn = resolve_run_host(None, repo=None, config=config, store=store)
    assert conn == RunHostConnection(url="http://ios", token="ios-tok")


def test_ambiguous_when_multiple_roles_and_none_specified(tmp_path):
    store = RunHostStore(tmp_path)
    config = _config(["ios-sim-host", "android-emu-host"])
    repo = _repo(run_host=None)

    with pytest.raises(RunHostError) as exc_info:
        resolve_run_host(None, repo=repo, config=config, store=store)
    msg = str(exc_info.value)
    assert "ios-sim-host" in msg and "android-emu-host" in msg
    assert "--remote" in msg


def test_unknown_role_not_in_config_run_hosts(tmp_path):
    store = RunHostStore(tmp_path)
    store.set("ghost-host", RunHostConnection(url="http://ghost", token="ghost-tok"))
    config = _config(["ios-sim-host"])
    repo = _repo(run_host=None)

    with pytest.raises(RunHostError) as exc_info:
        resolve_run_host("ghost-host", repo=repo, config=config, store=store)
    assert "ghost-host" in str(exc_info.value)


def test_unknown_role_from_repo_run_host(tmp_path):
    store = RunHostStore(tmp_path)
    config = _config(["ios-sim-host"])
    repo = _repo(run_host="typo-host")

    with pytest.raises(RunHostError) as exc_info:
        resolve_run_host(None, repo=repo, config=config, store=store)
    assert "typo-host" in str(exc_info.value)


def test_role_declared_but_not_mapped_in_store_names_the_fix(tmp_path):
    store = RunHostStore(tmp_path)
    config = _config(["ios-sim-host"])
    repo = _repo(run_host=None)

    with pytest.raises(RunHostError) as exc_info:
        resolve_run_host(None, repo=repo, config=config, store=store)
    msg = str(exc_info.value)
    assert "ios-sim-host" in msg
    assert "mship run-host add ios-sim-host" in msg


def test_no_run_hosts_declared_at_all_raises_actionable_error(tmp_path):
    store = RunHostStore(tmp_path)
    config = _config([])
    repo = _repo(run_host=None)

    with pytest.raises(RunHostError) as exc_info:
        resolve_run_host(None, repo=repo, config=config, store=store)
    assert "run_hosts" in str(exc_info.value)
