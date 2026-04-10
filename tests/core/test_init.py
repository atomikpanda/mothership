from pathlib import Path

import pytest

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
