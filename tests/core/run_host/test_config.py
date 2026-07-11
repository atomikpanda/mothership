# tests/core/run_host/test_config.py
"""WorkspaceConfig.run_hosts / RepoConfig.run_host parsing.

Two-layer run-host model: mothership.yaml (public) only ever declares logical
role names (`run_hosts: [...]`) and lets a repo opt into one (`run_host: ...`).
The concrete {url, token} connection for a role lives in the gitignored
`.mothership/run-hosts.yaml` store (see test_store.py / test_resolve.py).
"""
from pathlib import Path

from mship.core.config import ConfigLoader, RepoConfig, WorkspaceConfig


def test_run_hosts_default_empty():
    config = WorkspaceConfig(workspace="test", repos={})
    assert config.run_hosts == []


def test_run_hosts_parses_list():
    config = WorkspaceConfig(
        workspace="test",
        run_hosts=["ios-sim-host", "android-emu-host"],
        repos={},
    )
    assert config.run_hosts == ["ios-sim-host", "android-emu-host"]


def test_repo_run_host_default_none():
    repo = RepoConfig(path=Path("."), type="library")
    assert repo.run_host is None


def test_repo_run_host_parses():
    repo = RepoConfig(path=Path("."), type="library", run_host="ios-sim-host")
    assert repo.run_host == "ios-sim-host"


def test_config_loader_parses_run_hosts_and_repo_run_host(tmp_path: Path):
    """End-to-end through ConfigLoader.load, mirroring the relay config test."""
    repo_dir = tmp_path / "ios-app"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  test:\n    cmds:\n      - echo ok\n"
    )

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test-run-hosts
run_hosts: [ios-sim-host, android-emu-host]
repos:
  ios-app:
    path: ./ios-app
    type: service
    run_host: ios-sim-host
"""
    )

    config = ConfigLoader.load(cfg)
    assert config.run_hosts == ["ios-sim-host", "android-emu-host"]
    assert config.repos["ios-app"].run_host == "ios-sim-host"


def test_config_loader_run_hosts_absent_defaults_empty(tmp_path: Path):
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  test:\n    cmds:\n      - echo ok\n"
    )

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test-no-run-hosts
repos:
  myrepo:
    path: ./myrepo
    type: service
"""
    )

    config = ConfigLoader.load(cfg)
    assert config.run_hosts == []
    assert config.repos["myrepo"].run_host is None
