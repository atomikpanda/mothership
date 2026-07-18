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
    from mship.core.config import Dependency
    assert config.repos["auth"].depends_on == [Dependency(repo="shared", type="compile")]


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


def test_taskfile_template_has_no_colon_in_echo_strings(tmp_path: Path):
    """go-task 3.49.1 rejects colons inside echo strings. Template must not use them."""
    repo = tmp_path / "my-repo"
    repo.mkdir()
    init = WorkspaceInitializer()
    init.write_taskfile(repo)
    content = (repo / "Taskfile.yml").read_text()
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- echo"):
            assert "TODO:" not in stripped, f"Colon found in echo: {stripped}"


def test_write_taskfile_does_not_overwrite(tmp_path: Path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    existing = repo / "Taskfile.yml"
    existing.write_text("original content")
    init = WorkspaceInitializer()
    init.write_taskfile(repo)
    assert existing.read_text() == "original content"


def test_taskfile_template_commands_all_exit_nonzero():
    """ac3: every generated task fails (exit 1) — an unedited stub can NEVER
    fabricate a passing `mship test`; no exit-0 `echo` no-ops remain."""
    tmpl = WorkspaceInitializer.TASKFILE_TEMPLATE
    assert "exit 1" in tmpl
    assert "echo" not in tmpl
    parsed = yaml.safe_load(tmpl)
    for task_name in ("test", "run", "lint", "setup"):
        assert parsed["tasks"][task_name]["cmds"] == ["exit 1"], task_name


@pytest.mark.parametrize("fname", [
    "Taskfile.yml", "Taskfile.yaml", "taskfile.yml", "taskfile.yaml",
    "Taskfile.dist.yml", "Taskfile.dist.yaml", "taskfile.dist.yml", "taskfile.dist.yaml",
])
def test_write_taskfile_suppressed_by_existing_go_task_file(tmp_path: Path, fname: str):
    """ac1/ac2: an existing go-task file (any resolution-set spelling) suppresses
    the stub; NO shadowing Taskfile.yml is written; result reports the existing file."""
    repo = tmp_path / "svc"; repo.mkdir()
    (repo / fname).write_text("version: '3'\ntasks: {}\n")
    result = WorkspaceInitializer().write_taskfile(repo)
    assert result.wrote is False
    assert result.existing is not None and result.existing.name == fname
    if fname != "Taskfile.yml":
        assert not (repo / "Taskfile.yml").exists()   # no shadow stub written
        assert result.needs_rename is True


def test_write_taskfile_writes_when_absent(tmp_path: Path):
    repo = tmp_path / "svc"; repo.mkdir()
    result = WorkspaceInitializer().write_taskfile(repo)
    assert result.wrote is True
    assert (repo / "Taskfile.yml").exists()
    assert result.needs_rename is False


def test_detect_ignores_lone_generated_stub_taskfile(tmp_path: Path):
    """ac6: a dir whose ONLY marker is a mship-generated stub Taskfile is NOT
    promoted (so re-running `init --detect` ignores mship's own stubs)."""
    init = WorkspaceInitializer()
    stub_dir = tmp_path / "stubonly"; stub_dir.mkdir()
    (stub_dir / "Taskfile.yml").write_text(init.TASKFILE_TEMPLATE)
    assert "stubonly" not in [r.path.name for r in init.detect_repos(tmp_path)]


def test_detect_keeps_handwritten_taskfile(tmp_path: Path):
    init = WorkspaceInitializer()
    real = tmp_path / "realsvc"; real.mkdir()
    (real / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  test:\n    cmds:\n      - pytest\n"
    )
    repos = init.detect_repos(tmp_path)
    svc = next(r for r in repos if r.path.name == "realsvc")
    assert "Taskfile.yml" in svc.markers


def test_detect_stub_dir_with_other_marker_still_promoted(tmp_path: Path):
    """A stub Taskfile PLUS a real marker (.git) is still a repo — only a LONE
    stub is ignored, and the stub itself is not counted among the markers."""
    init = WorkspaceInitializer()
    d = tmp_path / "svc"; d.mkdir()
    (d / ".git").mkdir()
    (d / "Taskfile.yml").write_text(init.TASKFILE_TEMPLATE)
    svc = next(r for r in init.detect_repos(tmp_path) if r.path.name == "svc")
    assert ".git" in svc.markers
    assert "Taskfile.yml" not in svc.markers


def test_generate_config_passes_git_root(tmp_path: Path):
    """git_root from the repo dict must land on the RepoConfig (init --detect
    monorepo emission). See spec mship-init-detect-monorepo ac1."""
    init = WorkspaceInitializer()
    config = init.generate_config(
        workspace_name="mono",
        repos=[
            {"name": "root", "path": ".", "type": "service", "depends_on": []},
            {"name": "web", "path": "web", "type": "service",
             "git_root": "root", "depends_on": []},
        ],
        env_runner=None,
    )
    assert config.repos["root"].git_root is None
    assert config.repos["web"].git_root == "root"


def test_write_config_emits_git_root(tmp_path: Path):
    """write_config serializes git_root for subdir children and omits it for
    standalone repos. See spec mship-init-detect-monorepo ac1."""
    init = WorkspaceInitializer()
    config = init.generate_config(
        workspace_name="mono",
        repos=[
            {"name": "root", "path": ".", "type": "service", "depends_on": []},
            {"name": "web", "path": "web", "type": "service",
             "git_root": "root", "depends_on": []},
        ],
        env_runner=None,
    )
    out = tmp_path / "mothership.yaml"
    init.write_config(out, config)
    data = yaml.safe_load(out.read_text())
    assert data["repos"]["web"]["git_root"] == "root"
    assert data["repos"]["web"]["path"] == "web"
    assert data["repos"]["root"]["path"] == "."
    assert "git_root" not in data["repos"]["root"]
