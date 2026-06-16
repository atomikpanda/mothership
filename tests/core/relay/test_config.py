# tests/core/relay/test_config.py
from pathlib import Path

import pytest

from mship.core.relay.config import RelayConfig
from mship.core.config import ConfigLoader


def test_from_mapping_full():
    rc = RelayConfig.from_mapping({"host": "relay.example.com", "ssh_port": 2222, "user": "tunnel"})
    assert rc.host == "relay.example.com"
    assert rc.ssh_port == 2222
    assert rc.user == "tunnel"

def test_from_mapping_defaults_and_none():
    assert RelayConfig.from_mapping(None) is None           # no relay configured
    rc = RelayConfig.from_mapping({"host": "r.example.com"})
    assert rc.ssh_port == 2222 and rc.user is None          # defaults


def test_config_loader_parses_relay_block(tmp_path: Path):
    """ConfigLoader.load exposes config.relay.host when a relay: block is present."""
    # Create a minimal repo so ConfigLoader doesn't fail path validation
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks:\n  test:\n    cmds:\n      - echo ok\n")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test-relay
relay:
  host: relay.example.com
  ssh_port: 2222
  user: tunnel
repos:
  myrepo:
    path: ./myrepo
    type: service
"""
    )

    config = ConfigLoader.load(cfg)
    assert config.relay is not None
    assert config.relay.host == "relay.example.com"
    assert config.relay.ssh_port == 2222
    assert config.relay.user == "tunnel"


def test_config_loader_relay_none_when_absent(tmp_path: Path):
    """WorkspaceConfig.relay is None when no relay: block is in mothership.yaml."""
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks:\n  test:\n    cmds:\n      - echo ok\n")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test-no-relay
repos:
  myrepo:
    path: ./myrepo
    type: service
"""
    )

    config = ConfigLoader.load(cfg)
    assert config.relay is None
