from pathlib import Path

import pytest
import yaml

from mship.core.init import WorkspaceInitializer, DetectedRepo


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    """Create a directory with some repo-like subdirectories."""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / ".git").mkdir()
    (frontend / "package.json").write_text("{}")

    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / ".git").mkdir()
    (backend / "go.mod").write_text("module backend")

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / ".git").mkdir()
    (shared / "Taskfile.yml").write_text("version: '3'")

    docs = tmp_path / "docs"
    docs.mkdir()

    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / ".git").mkdir()

    nm = tmp_path / "node_modules"
    nm.mkdir()

    return tmp_path


def test_detect_repos(workspace_dir: Path):
    init = WorkspaceInitializer()
    repos = init.detect_repos(workspace_dir)
    names = [r.path.name for r in repos]
    assert "frontend" in names
    assert "backend" in names
    assert "shared" in names
    assert "docs" not in names
    assert ".hidden" not in names
    assert "node_modules" not in names


def test_detect_repos_markers(workspace_dir: Path):
    init = WorkspaceInitializer()
    repos = init.detect_repos(workspace_dir)
    frontend = next(r for r in repos if r.path.name == "frontend")
    assert ".git" in frontend.markers
    assert "package.json" in frontend.markers


def test_detect_repos_current_dir(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
    init = WorkspaceInitializer()
    repos = init.detect_repos(tmp_path)
    assert any(r.path == tmp_path for r in repos)


def test_detect_repos_empty_dir(tmp_path: Path):
    init = WorkspaceInitializer()
    repos = init.detect_repos(tmp_path)
    assert repos == []


def test_generate_config(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "Taskfile.yml").write_text("version: '3'")

    init = WorkspaceInitializer()
    config = init.generate_config(
        workspace_name="test-platform",
        repos=[
            {"name": "shared", "path": shared, "type": "library", "depends_on": []},
        ],
        env_runner=None,
    )
    assert config.workspace == "test-platform"
    assert "shared" in config.repos
    assert config.repos["shared"].type == "library"


def test_generate_config_with_deps(tmp_path: Path):
    for name in ["shared", "auth"]:
        d = tmp_path / name
        d.mkdir()
        (d / "Taskfile.yml").write_text("version: '3'")

    init = WorkspaceInitializer()
    config = init.generate_config(
        workspace_name="test",
        repos=[
            {"name": "shared", "path": tmp_path / "shared", "type": "library", "depends_on": []},
            {"name": "auth", "path": tmp_path / "auth", "type": "service", "depends_on": ["shared"]},
        ],
        env_runner=None,
    )
    assert config.repos["auth"].depends_on == ["shared"]


def test_generate_config_with_env_runner(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "Taskfile.yml").write_text("version: '3'")

    init = WorkspaceInitializer()
    config = init.generate_config(
        workspace_name="test",
        repos=[
            {"name": "shared", "path": shared, "type": "library", "depends_on": []},
        ],
        env_runner="dotenvx run --",
    )
    assert config.env_runner == "dotenvx run --"


def test_generate_config_circular_dep_raises(tmp_path: Path):
    for name in ["a", "b"]:
        d = tmp_path / name
        d.mkdir()
        (d / "Taskfile.yml").write_text("version: '3'")

    init = WorkspaceInitializer()
    with pytest.raises(ValueError, match="[Cc]ircular"):
        init.generate_config(
            workspace_name="test",
            repos=[
                {"name": "a", "path": tmp_path / "a", "type": "library", "depends_on": ["b"]},
                {"name": "b", "path": tmp_path / "b", "type": "library", "depends_on": ["a"]},
            ],
            env_runner=None,
        )


def test_write_config(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "Taskfile.yml").write_text("version: '3'")

    init = WorkspaceInitializer()
    config = init.generate_config(
        workspace_name="test",
        repos=[
            {"name": "shared", "path": shared, "type": "library", "depends_on": []},
        ],
        env_runner=None,
    )
    output = tmp_path / "mothership.yaml"
    init.write_config(output, config)
    assert output.exists()
    with open(output) as f:
        data = yaml.safe_load(f)
    assert data["workspace"] == "test"
    assert "shared" in data["repos"]


def test_write_taskfile(tmp_path: Path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    init = WorkspaceInitializer()
    init.write_taskfile(repo)
    tf = repo / "Taskfile.yml"
    assert tf.exists()
    content = tf.read_text()
    assert "test:" in content
    assert "run:" in content
    assert "lint:" in content
    assert "setup:" in content


def test_write_taskfile_does_not_overwrite(tmp_path: Path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    existing = repo / "Taskfile.yml"
    existing.write_text("original content")
    init = WorkspaceInitializer()
    init.write_taskfile(repo)
    assert existing.read_text() == "original content"
