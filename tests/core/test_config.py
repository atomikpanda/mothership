from pathlib import Path

import pytest

from mship.core.config import WorkspaceConfig, ConfigLoader, Dependency, Healthcheck


def test_load_minimal_config(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.workspace == "test-platform"
    assert len(config.repos) == 3
    assert config.repos["shared"].type == "library"
    assert len(config.repos["auth-service"].depends_on) == 1
    assert config.repos["auth-service"].depends_on[0].repo == "shared"


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


def test_dependency_model():
    dep = Dependency(repo="shared", type="compile")
    assert dep.repo == "shared"
    assert dep.type == "compile"


def test_dependency_default_type():
    dep = Dependency(repo="shared")
    assert dep.type == "compile"


def test_depends_on_string_normalized(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    deps = config.repos["auth-service"].depends_on
    assert len(deps) == 1
    assert isinstance(deps[0], Dependency)
    assert deps[0].repo == "shared"
    assert deps[0].type == "compile"


def test_depends_on_mixed_format(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text("""\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
  backend:
    path: ./auth-service
    type: service
  ios-app:
    path: ./api-gateway
    type: service
    depends_on:
      - repo: shared
        type: compile
      - repo: backend
        type: runtime
""")
    config = ConfigLoader.load(cfg)
    deps = config.repos["ios-app"].depends_on
    assert len(deps) == 2
    assert deps[0].repo == "shared"
    assert deps[0].type == "compile"
    assert deps[1].repo == "backend"
    assert deps[1].type == "runtime"


def test_tags_default_empty(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.repos["shared"].tags == []


def test_tags_loaded(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text("""\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
    tags: [apple, core]
""")
    config = ConfigLoader.load(cfg)
    assert config.repos["shared"].tags == ["apple", "core"]


def test_git_root_field_default_none(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.repos["shared"].git_root is None


def test_start_mode_default_foreground(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.repos["shared"].start_mode == "foreground"


def test_git_root_with_subdir(tmp_path: Path):
    """A monorepo config with a git_root subdirectory service."""
    root = tmp_path / "monorepo"
    root.mkdir()
    (root / "Taskfile.yml").write_text("version: '3'")
    web = root / "web"
    web.mkdir()
    (web / "Taskfile.yml").write_text("version: '3'")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: mono
repos:
  root:
    path: ./monorepo
    type: service
  web:
    path: web
    type: service
    git_root: root
    depends_on: [root]
"""
    )
    config = ConfigLoader.load(cfg)
    assert config.repos["web"].git_root == "root"
    # The path should remain as-is (not resolved to absolute) when git_root is set
    assert str(config.repos["web"].path) == "web"


def test_git_root_invalid_ref_raises(tmp_path: Path):
    root = tmp_path / "monorepo"
    root.mkdir()
    (root / "Taskfile.yml").write_text("version: '3'")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: mono
repos:
  root:
    path: ./monorepo
    type: service
  web:
    path: web
    type: service
    git_root: nonexistent
"""
    )
    with pytest.raises(ValueError, match="nonexistent"):
        ConfigLoader.load(cfg)


def test_git_root_cannot_chain(tmp_path: Path):
    """A git_root service cannot reference another git_root service."""
    root = tmp_path / "monorepo"
    root.mkdir()
    (root / "Taskfile.yml").write_text("version: '3'")
    (root / "web").mkdir()
    (root / "web" / "Taskfile.yml").write_text("version: '3'")
    (root / "web" / "admin").mkdir()
    (root / "web" / "admin" / "Taskfile.yml").write_text("version: '3'")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: mono
repos:
  root:
    path: ./monorepo
    type: service
  web:
    path: web
    type: service
    git_root: root
  admin:
    path: web/admin
    type: service
    git_root: web
"""
    )
    with pytest.raises(ValueError, match="chain"):
        ConfigLoader.load(cfg)


def test_git_root_missing_subdir_raises(tmp_path: Path):
    root = tmp_path / "monorepo"
    root.mkdir()
    (root / "Taskfile.yml").write_text("version: '3'")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: mono
repos:
  root:
    path: ./monorepo
    type: service
  web:
    path: web
    type: service
    git_root: root
"""
    )
    with pytest.raises(ValueError, match="web"):
        ConfigLoader.load(cfg)


def test_start_mode_background(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    start_mode: background
"""
    )
    config = ConfigLoader.load(cfg)
    assert config.repos["shared"].start_mode == "background"


def test_symlink_dirs_default_empty(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.repos["shared"].symlink_dirs == []


def test_symlink_dirs_loaded(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    symlink_dirs: [node_modules, .venv]
"""
    )
    config = ConfigLoader.load(cfg)
    assert config.repos["shared"].symlink_dirs == ["node_modules", ".venv"]


def test_healthcheck_tcp_probe():
    hc = Healthcheck(tcp="127.0.0.1:8001")
    assert hc.tcp == "127.0.0.1:8001"
    assert hc.http is None
    assert hc.timeout == "30s"
    assert hc.retry_interval == "500ms"


def test_healthcheck_requires_exactly_one_probe():
    with pytest.raises(ValueError, match="exactly one"):
        Healthcheck()
    with pytest.raises(ValueError, match="exactly one"):
        Healthcheck(tcp="127.0.0.1:8001", http="http://localhost/health")


def test_healthcheck_custom_timeout_and_interval():
    hc = Healthcheck(tcp="127.0.0.1:8001", timeout="60s", retry_interval="1s")
    assert hc.timeout == "60s"
    assert hc.retry_interval == "1s"


def test_repo_healthcheck_default_none(workspace):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.repos["shared"].healthcheck is None


def test_repo_healthcheck_loaded(workspace):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    healthcheck:
      tcp: "127.0.0.1:8001"
      timeout: 45s
"""
    )
    config = ConfigLoader.load(cfg)
    hc = config.repos["shared"].healthcheck
    assert hc is not None
    assert hc.tcp == "127.0.0.1:8001"
    assert hc.timeout == "45s"


def test_repo_healthcheck_http(workspace):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    healthcheck:
      http: "http://localhost:8000/health"
"""
    )
    config = ConfigLoader.load(cfg)
    assert config.repos["shared"].healthcheck.http == "http://localhost:8000/health"


def test_repo_healthcheck_task(workspace):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    healthcheck:
      task: wait-for-db
      timeout: 60s
"""
    )
    config = ConfigLoader.load(cfg)
    hc = config.repos["shared"].healthcheck
    assert hc.task == "wait-for-db"
    assert hc.timeout == "60s"
