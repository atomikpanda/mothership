from pathlib import Path

import pytest

from mship.core.config import WorkspaceConfig, ConfigLoader


def test_load_minimal_config(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.workspace == "test-platform"
    assert len(config.repos) == 3
    assert config.repos["shared"].type == "library"
    assert config.repos["auth-service"].depends_on == ["shared"]


def test_paths_resolved_relative_to_workspace(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.repos["shared"].path == workspace / "shared"
    assert config.repos["auth-service"].path == workspace / "auth-service"


def test_default_branch_pattern(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.branch_pattern == "feat/{slug}"


def test_custom_branch_pattern(workspace: Path):
    cfg = workspace / "mothership.yaml"
    content = cfg.read_text()
    cfg.write_text(content + 'branch_pattern: "mship/{slug}"\n')
    config = ConfigLoader.load(cfg)
    assert config.branch_pattern == "mship/{slug}"


def test_env_runner_defaults_to_none(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.env_runner is None


def test_invalid_depends_on_raises(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
    depends_on: [nonexistent]
"""
    )
    with pytest.raises(ValueError, match="nonexistent"):
        ConfigLoader.load(cfg)


def test_circular_dependency_raises(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  a:
    path: ./shared
    type: library
    depends_on: [b]
  b:
    path: ./auth-service
    type: library
    depends_on: [a]
"""
    )
    with pytest.raises(ValueError, match="[Cc]ircular"):
        ConfigLoader.load(cfg)


def test_missing_taskfile_raises(tmp_path: Path):
    cfg = tmp_path / "mothership.yaml"
    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()
    cfg.write_text(
        f"""\
workspace: test
repos:
  empty:
    path: ./empty
    type: library
"""
    )
    with pytest.raises(ValueError, match="Taskfile"):
        ConfigLoader.load(cfg)


def test_discover_walks_up(workspace: Path):
    subdir = workspace / "shared" / "src"
    subdir.mkdir(parents=True, exist_ok=True)
    found = ConfigLoader.discover(subdir)
    assert found == workspace / "mothership.yaml"


def test_discover_not_found_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        ConfigLoader.discover(tmp_path)


def test_task_name_override(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
    tasks:
      test: unit
"""
    )
    config = ConfigLoader.load(cfg)
    assert config.repos["shared"].tasks == {"test": "unit"}
