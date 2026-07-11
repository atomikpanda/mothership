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


def _write_repo(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")


def test_repo_config_accepts_drift_fields(tmp_path):
    import yaml
    from mship.core.config import ConfigLoader

    _write_repo(tmp_path, "cli")
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "repos": {
            "cli": {
                "path": "./cli", "type": "service",
                "expected_branch": "main",
                "allow_dirty": True,
                "allow_extra_worktrees": True,
            },
        },
    }))
    cfg = ConfigLoader.load(cfg_path)
    r = cfg.repos["cli"]
    assert r.expected_branch == "main"
    assert r.allow_dirty is True
    assert r.allow_extra_worktrees is True


def test_repo_config_drift_defaults(tmp_path):
    import yaml
    from mship.core.config import ConfigLoader

    _write_repo(tmp_path, "cli")
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "repos": {"cli": {"path": "./cli", "type": "service"}},
    }))
    cfg = ConfigLoader.load(cfg_path)
    r = cfg.repos["cli"]
    assert r.expected_branch is None
    assert r.allow_dirty is False
    assert r.allow_extra_worktrees is False


def test_audit_policy_defaults_to_blocking(tmp_path):
    import yaml
    from mship.core.config import ConfigLoader

    _write_repo(tmp_path, "cli")
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "repos": {"cli": {"path": "./cli", "type": "service"}},
    }))
    cfg = ConfigLoader.load(cfg_path)
    assert cfg.audit.block_spawn is True
    assert cfg.audit.block_finish is True


def test_audit_policy_opt_out(tmp_path):
    import yaml
    from mship.core.config import ConfigLoader

    _write_repo(tmp_path, "cli")
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "audit": {"block_spawn": False, "block_finish": False},
        "repos": {"cli": {"path": "./cli", "type": "service"}},
    }))
    cfg = ConfigLoader.load(cfg_path)
    assert cfg.audit.block_spawn is False
    assert cfg.audit.block_finish is False


def test_expected_branch_conflict_rejected(tmp_path):
    import yaml
    import pytest
    from mship.core.config import ConfigLoader

    _write_repo(tmp_path, "mono")
    _write_repo(tmp_path, "mono/pkg-a")
    _write_repo(tmp_path, "mono/pkg-b")
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "repos": {
            "mono": {"path": "./mono", "type": "service"},
            "pkg_a": {"path": "pkg-a", "type": "library", "git_root": "mono",
                       "expected_branch": "main"},
            "pkg_b": {"path": "pkg-b", "type": "library", "git_root": "mono",
                       "expected_branch": "develop"},
        },
    }))
    with pytest.raises(ValueError, match="expected_branch"):
        ConfigLoader.load(cfg_path)


def test_repo_config_accepts_base_branch(tmp_path):
    import yaml
    from mship.core.config import ConfigLoader

    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    (cli_dir / "Taskfile.yml").write_text("version: '3'")
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    (api_dir / "Taskfile.yml").write_text("version: '3'")

    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "repos": {
            "cli": {"path": "./cli", "type": "service", "base_branch": "main"},
            "api": {"path": "./api", "type": "service"},
        },
    }))
    cfg = ConfigLoader.load(cfg_path)
    assert cfg.repos["cli"].base_branch == "main"
    assert cfg.repos["api"].base_branch is None


# --- bind_files validation (issue #39) ---

def test_bind_files_accepts_relative_paths(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - .env\n"
        "      - .vscode/settings.local.json\n"
        "      - apps/**/.env\n"
    )
    cfg = ConfigLoader.load(cfg_path)
    assert cfg.repos["r"].bind_files == [
        ".env",
        ".vscode/settings.local.json",
        "apps/**/.env",
    ]


def test_bind_files_rejects_absolute_path(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - /etc/secrets\n"
    )
    with pytest.raises(Exception) as exc:
        ConfigLoader.load(cfg_path)
    assert "/etc/secrets" in str(exc.value)
    assert "absolute" in str(exc.value).lower()


def test_bind_files_rejects_parent_escape(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - ../other-repo/.env\n"
    )
    with pytest.raises(Exception) as exc:
        ConfigLoader.load(cfg_path)
    assert "../other-repo/.env" in str(exc.value) or ".." in str(exc.value)


def test_bind_files_empty_list_is_default(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
    )
    cfg = ConfigLoader.load(cfg_path)
    assert cfg.repos["r"].bind_files == []


def test_discover_env_var_valid(tmp_path, monkeypatch):
    from mship.core.config import ConfigLoader
    root = tmp_path / "ws"
    root.mkdir()
    (root / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    other = tmp_path / "other"; other.mkdir()
    monkeypatch.setenv("MSHIP_WORKSPACE", str(root))
    path = ConfigLoader.discover(other)
    assert path == root / "mothership.yaml"


def test_discover_env_var_invalid_raises(tmp_path, monkeypatch):
    from mship.core.config import ConfigLoader
    import pytest
    monkeypatch.setenv("MSHIP_WORKSPACE", str(tmp_path / "does-not-exist"))
    with pytest.raises(FileNotFoundError) as exc:
        ConfigLoader.discover(tmp_path)
    assert "MSHIP_WORKSPACE" in str(exc.value)


def test_discover_marker_precedes_walk_up(tmp_path, monkeypatch):
    """Marker at worktree points to root A; walk-up would find root B.
    Marker wins."""
    from mship.core.config import ConfigLoader
    from mship.core.workspace_marker import write_marker
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)

    root_a = tmp_path / "a-ws"; root_a.mkdir()
    (root_a / "mothership.yaml").write_text("workspace: a\nrepos: {}\n")

    root_b = tmp_path / "b-ws"; root_b.mkdir()
    (root_b / "mothership.yaml").write_text("workspace: b\nrepos: {}\n")
    worktree = root_b / "wt"; worktree.mkdir()
    write_marker(worktree, root_a)

    path = ConfigLoader.discover(worktree)
    assert path == root_a / "mothership.yaml"


def test_discover_stale_marker_falls_through_to_walk_up(tmp_path, monkeypatch):
    from mship.core.config import ConfigLoader
    from mship.core.workspace_marker import MARKER_NAME
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)

    root = tmp_path / "ws"; root.mkdir()
    (root / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    worktree = root / "sub"; worktree.mkdir()
    (worktree / MARKER_NAME).write_text(str(tmp_path / "nope"))

    path = ConfigLoader.discover(worktree)
    assert path == root / "mothership.yaml"


def test_discover_walk_up_unchanged_when_no_env_no_marker(tmp_path, monkeypatch):
    """Regression: existing behavior works when env var and marker both absent."""
    from mship.core.config import ConfigLoader
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    root = tmp_path / "ws"; root.mkdir()
    (root / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    nested = root / "a" / "b"; nested.mkdir(parents=True)
    assert ConfigLoader.discover(nested) == root / "mothership.yaml"


def test_docs_dir_defaults_to_docs(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.docs_dir == "docs"


def test_custom_docs_dir(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(cfg.read_text() + 'docs_dir: "documentation"\n')
    config = ConfigLoader.load(cfg)
    assert config.docs_dir == "documentation"


def test_repo_url_and_default_remote_parse():
    cfg = WorkspaceConfig(
        workspace="w",
        default_remote="https://github.com/atomikpanda",
        repos={
            "lib": {"path": "lib", "type": "library", "url": "atomikpanda/lib"},
            "svc": {"path": "svc", "type": "service"},  # url omitted is allowed
        },
    )
    assert cfg.default_remote == "https://github.com/atomikpanda"
    assert cfg.repos["lib"].url == "atomikpanda/lib"
    assert cfg.repos["svc"].url is None


def test_repo_url_rejects_blank():
    with pytest.raises(ValueError):
        WorkspaceConfig(
            workspace="w",
            repos={"lib": {"path": "lib", "type": "library", "url": "   "}},
        )


def test_repo_url_is_stripped_at_parse():
    cfg = WorkspaceConfig(
        workspace="w",
        repos={"lib": {"path": "lib", "type": "library", "url": "  atomikpanda/lib  "}},
    )
    assert cfg.repos["lib"].url == "atomikpanda/lib"


def test_load_lenient_skips_missing_paths(tmp_path):
    (tmp_path / "mothership.yaml").write_text(
        "workspace: w\n"
        "default_remote: https://github.com/atomikpanda\n"
        "repos:\n"
        "  lib:\n"
        "    path: lib\n"
        "    type: library\n"
    )
    cfg = ConfigLoader.load(tmp_path / "mothership.yaml", require_paths=False)
    assert "lib" in cfg.repos
    assert cfg.repos["lib"].path == (tmp_path / "lib").resolve()  # still resolved


def test_load_lenient_still_raises_on_schema_error(tmp_path):
    # Dependency cycle is a pure schema error — must raise even when lenient.
    (tmp_path / "mothership.yaml").write_text(
        "workspace: w\n"
        "repos:\n"
        "  a:\n    path: a\n    type: library\n    depends_on: [b]\n"
        "  b:\n    path: b\n    type: library\n    depends_on: [a]\n"
    )
    with pytest.raises(ValueError):
        ConfigLoader.load(tmp_path / "mothership.yaml", require_paths=False)


def test_load_strict_still_raises_on_missing_path(tmp_path):
    (tmp_path / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
    )
    with pytest.raises(ValueError):
        ConfigLoader.load(tmp_path / "mothership.yaml")  # default require_paths=True


def test_repo_capture_config_parses(tmp_path):
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        "    capture:\n"
        "      platforms: [android, ios]\n"
    )
    config = ConfigLoader.load(cfg, require_paths=False)
    assert config.repos["app"].capture is not None
    assert config.repos["app"].capture.platforms == ["android", "ios"]


def test_repo_without_capture_defaults_none(tmp_path):
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
    )
    config = ConfigLoader.load(cfg, require_paths=False)
    assert config.repos["app"].capture is None


def test_redact_null_patterns_coerces_to_empty_list(tmp_path):
    """`redact:\\n  patterns:` (no list under the key) parses as YAML null,
    not an omitted key — must coerce to `[]` rather than fail pydantic list
    validation and error the whole config load (MOS-102 Greptile fix)."""
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "redact:\n"
        "  patterns:\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
    )
    config = ConfigLoader.load(cfg, require_paths=False)
    assert config.redact is not None
    assert config.redact.patterns == []


# ---------------------------------------------------------------------------
# `hooks:` — lifecycle hooks config schema (MOS-220, spec mship-lifecycle-hooks)
# ---------------------------------------------------------------------------


def test_hooks_default_empty(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.hooks == []


def test_hooks_default_timeout_defaults_to_30(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.hooks_default_timeout == 30


def test_hooks_default_timeout_overridable(tmp_path):
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "hooks_default_timeout: 90\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
    )
    config = ConfigLoader.load(cfg, require_paths=False)
    assert config.hooks_default_timeout == 90


def test_hooks_parses_full_entry(tmp_path):
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        "hooks:\n"
        "  - on: pr.merged\n"
        "    run: notify-pr-merged\n"
        "    repo: app\n"
        "    name: \"Notify on PR merge\"\n"
        "    timeout: 45\n"
        "    required: false\n"
    )
    config = ConfigLoader.load(cfg, require_paths=False)
    assert len(config.hooks) == 1
    hook = config.hooks[0]
    assert hook.on == "pr.merged"
    assert hook.run == "notify-pr-merged"
    assert hook.repo == "app"
    assert hook.name == "Notify on PR merge"
    assert hook.timeout == 45
    assert hook.required is False


def test_hooks_entry_defaults(tmp_path):
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        "hooks:\n"
        "  - on: task.finished\n"
        "    run: some-task\n"
    )
    config = ConfigLoader.load(cfg, require_paths=False)
    hook = config.hooks[0]
    assert hook.repo is None
    assert hook.name is None
    assert hook.timeout is None
    assert hook.required is False


@pytest.mark.parametrize("event", [
    "task.finished", "task.closed", "pr.merged", "pr.closed",
    "phase.entered.plan", "phase.entered.dev", "phase.entered.review", "phase.entered.run",
    "workitem.phase.inbox", "workitem.phase.shaping", "workitem.phase.ready",
    "workitem.phase.in_flight", "workitem.phase.review", "workitem.phase.done",
])
def test_hooks_v1_event_catalog_accepted(tmp_path, event):
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        "hooks:\n"
        f"  - on: {event}\n"
        "    run: some-task\n"
    )
    config = ConfigLoader.load(cfg, require_paths=False)
    assert config.hooks[0].on == event


def test_hooks_unknown_event_rejected(tmp_path):
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        "hooks:\n"
        "  - on: task.exploded\n"
        "    run: some-task\n"
    )
    with pytest.raises(ValueError, match="task.exploded"):
        ConfigLoader.load(cfg, require_paths=False)


def test_hooks_empty_run_rejected(tmp_path):
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        "hooks:\n"
        "  - on: task.finished\n"
        "    run: \"\"\n"
    )
    with pytest.raises(ValueError):
        ConfigLoader.load(cfg, require_paths=False)


@pytest.mark.parametrize("event", ["task.finished", "task.closed", "pr.merged", "pr.closed"])
def test_hooks_required_true_rejected_on_post_hoc_events(tmp_path, event):
    """`required: true` can't block anything on task.finished/task.closed/
    pr.merged/pr.closed — each of these fires only AFTER its own irreversible
    side effects already landed (PRs/branches pushed, spec state advanced,
    worktree torn down, or simply observed after the fact by PrWatcher's
    poll), so a hook here can never abort the transition. See spec q5 and the
    MOS-220 PR review follow-up (required close/finish hooks leaving spec
    state advanced or PRs already created while the local transition is
    blocked)."""
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        "hooks:\n"
        f"  - on: {event}\n"
        "    run: some-task\n"
        "    required: true\n"
    )
    with pytest.raises(ValueError, match="required"):
        ConfigLoader.load(cfg, require_paths=False)


@pytest.mark.parametrize("event", [
    "phase.entered.plan", "phase.entered.dev", "phase.entered.review", "phase.entered.run",
    "workitem.phase.inbox", "workitem.phase.shaping", "workitem.phase.ready",
    "workitem.phase.in_flight", "workitem.phase.review", "workitem.phase.done",
])
def test_hooks_required_true_accepted_on_pre_mutation_events(tmp_path, event):
    """`required: true` IS meaningful on phase.entered.*/workitem.phase.* — both
    fire before their state mutation commits, so a required failure can
    genuinely abort the transition. These are the only event families where
    `required: true` is accepted."""
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        "hooks:\n"
        f"  - on: {event}\n"
        "    run: some-task\n"
        "    required: true\n"
    )
    config = ConfigLoader.load(cfg, require_paths=False)
    assert config.hooks[0].required is True


def test_hooks_unknown_repo_ref_rejected(tmp_path):
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        "hooks:\n"
        "  - on: task.finished\n"
        "    run: some-task\n"
        "    repo: nonexistent\n"
    )
    with pytest.raises(ValueError, match="nonexistent"):
        ConfigLoader.load(cfg, require_paths=False)

