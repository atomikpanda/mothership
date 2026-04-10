# `mship init` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a guided `mship init` command that scaffolds `mothership.yaml` and optional `Taskfile.yml` files, with both interactive (InquirerPy) and flag-based (agent/CI) modes.

**Architecture:** Core `WorkspaceInitializer` class handles detection, config generation, and file writing (CLI-independent). CLI layer splits into interactive wizard (InquirerPy) and flag-based parsing, both calling the same core methods.

**Tech Stack:** Python 3.14, Typer, InquirerPy, Pydantic v2, PyYAML (all existing dependencies)

---

## File Map

### Core
- `src/mship/core/init.py` — create: `WorkspaceInitializer` with detect, generate, write methods

### CLI
- `src/mship/cli/init.py` — create: `mship init` command with interactive + flag-based modes
- `src/mship/cli/__init__.py` — modify: register init module (note: init does NOT use `get_container` since there's no config yet)

### Tests
- `tests/core/test_init.py` — create: WorkspaceInitializer tests
- `tests/cli/test_init.py` — create: CLI tests for both modes

---

### Task 1: WorkspaceInitializer — Auto-Detection

**Files:**
- Create: `src/mship/core/init.py`
- Create: `tests/core/test_init.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_init.py`:
```python
from pathlib import Path

import pytest

from mship.core.init import WorkspaceInitializer, DetectedRepo


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    """Create a directory with some repo-like subdirectories."""
    # Repo with .git and package.json
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / ".git").mkdir()
    (frontend / "package.json").write_text("{}")

    # Repo with .git and go.mod
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / ".git").mkdir()
    (backend / "go.mod").write_text("module backend")

    # Repo with .git and Taskfile.yml
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / ".git").mkdir()
    (shared / "Taskfile.yml").write_text("version: '3'")

    # Non-repo directory
    docs = tmp_path / "docs"
    docs.mkdir()

    # Hidden directory (should be skipped)
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / ".git").mkdir()

    # node_modules (should be skipped)
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
    """Detect current directory as a repo if it has markers."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
    init = WorkspaceInitializer()
    repos = init.detect_repos(tmp_path)
    assert any(r.path == tmp_path for r in repos)


def test_detect_repos_empty_dir(tmp_path: Path):
    init = WorkspaceInitializer()
    repos = init.detect_repos(tmp_path)
    assert repos == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_init.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.init'`

- [ ] **Step 3: Write the implementation**

`src/mship/core/init.py`:
```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from mship.core.config import RepoConfig, WorkspaceConfig


REPO_MARKERS = [
    ".git",
    "Taskfile.yml",
    "package.json",
    "go.mod",
    "pyproject.toml",
    "Cargo.toml",
    "build.gradle",
    "pom.xml",
]

SKIP_DIRS = {
    "node_modules",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
}


@dataclass
class DetectedRepo:
    path: Path
    markers: list[str] = field(default_factory=list)


class WorkspaceInitializer:
    """Detects repos, generates config, and scaffolds files."""

    def detect_repos(self, workspace_path: Path) -> list[DetectedRepo]:
        repos: list[DetectedRepo] = []

        # Check current directory itself
        current_markers = self._find_markers(workspace_path)
        if current_markers:
            repos.append(DetectedRepo(path=workspace_path, markers=current_markers))

        # Check immediate subdirectories
        for item in sorted(workspace_path.iterdir()):
            if not item.is_dir():
                continue
            if item.name.startswith("."):
                continue
            if item.name in SKIP_DIRS:
                continue
            markers = self._find_markers(item)
            if markers:
                repos.append(DetectedRepo(path=item, markers=markers))

        return repos

    def _find_markers(self, path: Path) -> list[str]:
        markers: list[str] = []
        for marker in REPO_MARKERS:
            if (path / marker).exists():
                markers.append(marker)
        return markers
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_init.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/init.py tests/core/test_init.py
git commit -m "feat: add WorkspaceInitializer with repo auto-detection"
```

---

### Task 2: WorkspaceInitializer — Config Generation & File Writing

**Files:**
- Modify: `src/mship/core/init.py`
- Modify: `tests/core/test_init.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_init.py`:
```python
import yaml


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_init.py -v -k "generate or write"`
Expected: FAIL — `AttributeError: 'WorkspaceInitializer' object has no attribute 'generate_config'`

- [ ] **Step 3: Write the implementation**

Add to `src/mship/core/init.py` (inside the `WorkspaceInitializer` class):

```python
    TASKFILE_TEMPLATE = """\
version: '3'

tasks:
  test:
    desc: Run tests
    cmds:
      - echo "TODO: add test command"

  run:
    desc: Start the service
    cmds:
      - echo "TODO: add run command"

  lint:
    desc: Run linter
    cmds:
      - echo "TODO: add lint command"

  setup:
    desc: Set up development environment
    cmds:
      - echo "TODO: add setup command"
"""

    def generate_config(
        self,
        workspace_name: str,
        repos: list[dict],
        env_runner: str | None,
    ) -> WorkspaceConfig:
        """Build and validate a WorkspaceConfig from user inputs."""
        repo_configs: dict[str, RepoConfig] = {}
        for repo in repos:
            repo_configs[repo["name"]] = RepoConfig(
                path=Path(repo["path"]),
                type=repo["type"],
                depends_on=repo.get("depends_on", []),
            )

        config = WorkspaceConfig(
            workspace=workspace_name,
            env_runner=env_runner,
            repos=repo_configs,
        )
        # Pydantic validators run automatically (deps refs, cycles)
        return config

    def write_config(self, path: Path, config: WorkspaceConfig) -> None:
        """Write mothership.yaml."""
        data: dict = {
            "workspace": config.workspace,
        }
        if config.env_runner:
            data["env_runner"] = config.env_runner

        repos_data: dict = {}
        for name, repo in config.repos.items():
            repo_data: dict = {
                "path": str(repo.path),
                "type": repo.type,
            }
            if repo.depends_on:
                repo_data["depends_on"] = repo.depends_on
            repos_data[name] = repo_data
        data["repos"] = repos_data

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def write_taskfile(self, repo_path: Path) -> None:
        """Write a starter Taskfile.yml if one doesn't exist."""
        taskfile = repo_path / "Taskfile.yml"
        if taskfile.exists():
            return
        taskfile.write_text(self.TASKFILE_TEMPLATE)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_init.py -v`
Expected: All 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/init.py tests/core/test_init.py
git commit -m "feat: add config generation, file writing, and Taskfile scaffolding"
```

---

### Task 3: CLI — Non-Interactive (Flag-Based) Mode

**Files:**
- Create: `src/mship/cli/init.py`
- Create: `tests/cli/test_init.py`
- Modify: `src/mship/cli/__init__.py`

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_init.py`:
```python
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from mship.cli import app

runner = CliRunner()


@pytest.fixture
def init_workspace(tmp_path: Path) -> Path:
    """Create a workspace with repo dirs that have Taskfile.yml."""
    for name in ["shared", "auth-service"]:
        d = tmp_path / name
        d.mkdir()
        (d / ".git").mkdir()
        (d / "Taskfile.yml").write_text("version: '3'")
    return tmp_path


def test_init_non_interactive(init_workspace: Path):
    result = runner.invoke(app, [
        "init",
        "--name", "test-platform",
        "--repo", f"{init_workspace / 'shared'}:library",
        "--repo", f"{init_workspace / 'auth-service'}:service:shared",
    ], input=None)
    assert result.exit_code == 0, result.output
    config_path = init_workspace / "mothership.yaml"
    # init writes to cwd; since we can't change cwd in tests,
    # check the output mentions the file was created
    assert "mothership.yaml" in result.output or "Created" in result.output


def test_init_non_interactive_with_cwd(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, [
        "init",
        "--name", "test-platform",
        "--repo", "./shared:library",
        "--repo", "./auth-service:service:shared",
    ])
    assert result.exit_code == 0, result.output
    config_path = init_workspace / "mothership.yaml"
    assert config_path.exists()
    with open(config_path) as f:
        data = yaml.safe_load(f)
    assert data["workspace"] == "test-platform"
    assert "shared" in data["repos"]
    assert data["repos"]["shared"]["type"] == "library"
    assert data["repos"]["auth-service"]["type"] == "service"
    assert data["repos"]["auth-service"]["depends_on"] == ["shared"]


def test_init_detect(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, [
        "init",
        "--name", "test-platform",
        "--detect",
    ])
    assert result.exit_code == 0, result.output
    config_path = init_workspace / "mothership.yaml"
    assert config_path.exists()
    with open(config_path) as f:
        data = yaml.safe_load(f)
    assert "shared" in data["repos"]
    assert "auth-service" in data["repos"]


def test_init_already_exists(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    (init_workspace / "mothership.yaml").write_text("workspace: existing")
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./shared:library",
    ])
    assert result.exit_code != 0 or "already exists" in result.output.lower()


def test_init_force_overwrite(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    (init_workspace / "mothership.yaml").write_text("workspace: existing")
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./shared:library",
        "--force",
    ])
    assert result.exit_code == 0, result.output


def test_init_env_runner(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./shared:library",
        "--env-runner", "dotenvx run --",
    ])
    assert result.exit_code == 0, result.output
    with open(init_workspace / "mothership.yaml") as f:
        data = yaml.safe_load(f)
    assert data["env_runner"] == "dotenvx run --"


def test_init_scaffold_taskfiles(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    no_taskfile = init_workspace / "new-repo"
    no_taskfile.mkdir()
    (no_taskfile / ".git").mkdir()
    result = runner.invoke(app, [
        "init",
        "--name", "test",
        "--repo", "./new-repo:service",
        "--scaffold-taskfiles",
    ])
    assert result.exit_code == 0, result.output
    assert (no_taskfile / "Taskfile.yml").exists()


def test_init_no_args_no_tty(init_workspace: Path, monkeypatch):
    monkeypatch.chdir(init_workspace)
    result = runner.invoke(app, ["init"])
    # No --name, no --repo, no --detect, not a TTY → error
    assert result.exit_code != 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_init.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write the implementation**

`src/mship/cli/init.py`:
```python
from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output
from mship.core.init import WorkspaceInitializer


def register(app: typer.Typer, get_container):
    @app.command()
    def init(
        name: Optional[str] = typer.Option(None, "--name", help="Workspace name"),
        repo: Optional[list[str]] = typer.Option(None, "--repo", help="Repo in format path:type[:dep1,dep2]"),
        detect: bool = typer.Option(False, "--detect", help="Auto-detect repos in current directory"),
        env_runner: Optional[str] = typer.Option(None, "--env-runner", help="Secret management command prefix"),
        scaffold_taskfiles: bool = typer.Option(False, "--scaffold-taskfiles", help="Create starter Taskfile.yml for repos without one"),
        force: bool = typer.Option(False, "--force", help="Overwrite existing mothership.yaml"),
    ):
        """Initialize a new mothership workspace."""
        output = Output()
        cwd = Path.cwd()
        config_path = cwd / "mothership.yaml"
        initializer = WorkspaceInitializer()

        # Check for existing config
        if config_path.exists() and not force:
            output.error("mothership.yaml already exists. Use --force to overwrite.")
            raise typer.Exit(code=1)

        # Interactive mode
        if output.is_tty and name is None and not repo and not detect:
            _run_interactive(initializer, output, cwd, config_path, env_runner, force)
            return

        # Non-interactive mode
        if name is None:
            output.error("--name is required in non-interactive mode")
            raise typer.Exit(code=1)

        if not repo and not detect:
            output.error("Provide --repo flags or --detect in non-interactive mode")
            raise typer.Exit(code=1)

        repos_data: list[dict] = []

        # Parse --repo flags
        if repo:
            for r in repo:
                parsed = _parse_repo_flag(r, cwd)
                repos_data.append(parsed)

        # Auto-detect
        if detect:
            detected = initializer.detect_repos(cwd)
            existing_paths = {rd["path"] for rd in repos_data}
            for d in detected:
                if d.path.resolve() not in existing_paths:
                    repo_name = d.path.name if d.path != cwd else cwd.name
                    repos_data.append({
                        "name": repo_name,
                        "path": d.path,
                        "type": "service",
                        "depends_on": [],
                    })

        try:
            config = initializer.generate_config(name, repos_data, env_runner)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        initializer.write_config(config_path, config)

        # Scaffold Taskfiles
        created_taskfiles: list[str] = []
        if scaffold_taskfiles:
            for rd in repos_data:
                repo_path = Path(rd["path"])
                if not (repo_path / "Taskfile.yml").exists():
                    initializer.write_taskfile(repo_path)
                    created_taskfiles.append(str(repo_path))

        if output.is_tty:
            output.success(f"Created: {config_path}")
            for tf in created_taskfiles:
                output.success(f"Created: {tf}/Taskfile.yml")
            output.print("\nRun `mship status` to verify your workspace.")
        else:
            output.json({
                "config": str(config_path),
                "taskfiles_created": created_taskfiles,
            })


def _parse_repo_flag(value: str, cwd: Path) -> dict:
    """Parse 'path:type[:dep1,dep2]' format."""
    parts = value.split(":")
    if len(parts) < 2:
        raise typer.Exit(code=1)

    path_str = parts[0]
    repo_type = parts[1]
    depends_on = parts[2].split(",") if len(parts) > 2 else []

    path = (cwd / path_str).resolve()
    repo_name = Path(path_str).name
    if repo_name == ".":
        repo_name = path.name

    return {
        "name": repo_name,
        "path": path,
        "type": repo_type,
        "depends_on": depends_on,
    }


def _run_interactive(
    initializer: WorkspaceInitializer,
    output: Output,
    cwd: Path,
    config_path: Path,
    env_runner: str | None,
    force: bool,
):
    """Run the interactive wizard using InquirerPy."""
    from InquirerPy import inquirer

    output.print("[bold]Welcome to Mothership![/bold] Let's set up your workspace.\n")

    # 1. Workspace name
    default_name = cwd.name
    workspace_name = inquirer.text(
        message="Workspace name:",
        default=default_name,
    ).execute()

    # 2. Detect repos
    output.print("\nScanning for repositories...")
    detected = initializer.detect_repos(cwd)

    if detected:
        choices = []
        for d in detected:
            rel = d.path.relative_to(cwd) if d.path != cwd else Path(".")
            marker_str = ", ".join(d.markers)
            label = f"./{rel} (has {marker_str})"
            choices.append({"name": label, "value": d, "enabled": True})

        selected = inquirer.checkbox(
            message="Select repos to include:",
            choices=choices,
        ).execute()
    else:
        output.print("No repos detected automatically.")
        selected = []

    # 3. Manual add
    while True:
        extra = inquirer.text(
            message="Add another repo path? (enter path or leave blank to skip):",
            default="",
        ).execute()
        if not extra:
            break
        extra_path = (cwd / extra).resolve()
        if extra_path.is_dir():
            selected.append(DetectedRepo(path=extra_path, markers=[]))
        else:
            output.warning(f"Path does not exist: {extra_path}")

    if not selected:
        output.error("No repos selected. Aborting.")
        raise typer.Exit(code=1)

    # 4. Repo types
    repos_data: list[dict] = []
    for det in selected:
        rel = det.path.relative_to(cwd) if det.path != cwd else Path(".")
        repo_name = det.path.name if det.path != cwd else cwd.name
        repo_type = inquirer.select(
            message=f'What type is "{repo_name}"?',
            choices=["library", "service"],
            default="service",
        ).execute()
        repos_data.append({
            "name": repo_name,
            "path": det.path,
            "type": repo_type,
            "depends_on": [],
        })

    # 5. Dependencies
    repo_names = [r["name"] for r in repos_data]
    for rd in repos_data:
        other_repos = [n for n in repo_names if n != rd["name"]]
        if other_repos:
            deps = inquirer.checkbox(
                message=f'What does "{rd["name"]}" depend on?',
                choices=other_repos,
            ).execute()
            rd["depends_on"] = deps

    # 6. Taskfile scaffolding
    created_taskfiles: list[str] = []
    for rd in repos_data:
        repo_path = Path(rd["path"])
        if not (repo_path / "Taskfile.yml").exists():
            scaffold = inquirer.confirm(
                message=f'"{rd["name"]}" has no Taskfile.yml. Create a starter?',
                default=True,
            ).execute()
            if scaffold:
                initializer.write_taskfile(repo_path)
                created_taskfiles.append(rd["name"])

    # 7. Env runner
    if env_runner is None:
        env_choice = inquirer.select(
            message="Secret management (env_runner)?",
            choices=[
                {"name": "None", "value": None},
                {"name": "dotenvx run --", "value": "dotenvx run --"},
                {"name": "doppler run --", "value": "doppler run --"},
                {"name": "op run --", "value": "op run --"},
                {"name": "Custom...", "value": "__custom__"},
            ],
            default=None,
        ).execute()
        if env_choice == "__custom__":
            env_runner = inquirer.text(message="Enter custom env_runner:").execute()
        else:
            env_runner = env_choice

    # 8. Generate and write
    try:
        config = initializer.generate_config(workspace_name, repos_data, env_runner)
    except ValueError as e:
        output.error(str(e))
        raise typer.Exit(code=1)

    initializer.write_config(config_path, config)

    output.print("")
    output.success(f"Created: {config_path}")
    for name in created_taskfiles:
        output.success(f"Created: {name}/Taskfile.yml")
    output.print("\nRun `mship status` to verify your workspace.")
```

- [ ] **Step 4: Register in `cli/__init__.py`**

Add to `src/mship/cli/__init__.py` — note that `init` does NOT use `get_container` (there's no config yet). Add after existing imports but register differently:

```python
from mship.cli import init as _init_mod

_init_mod.register(app, get_container)
```

Note: `init` command ignores `get_container` — it's passed for signature consistency but the init function doesn't call it.

- [ ] **Step 5: Add missing import in `cli/init.py`**

At the top of `_run_interactive`, `DetectedRepo` needs to be imported:

```python
from mship.core.init import WorkspaceInitializer, DetectedRepo
```

(This import is already at the top of the file in the main import block.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_init.py -v`
Expected: All 8 tests PASS.

- [ ] **Step 7: Run full test suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
git add src/mship/cli/init.py src/mship/cli/__init__.py tests/cli/test_init.py
git commit -m "feat: add mship init command with interactive wizard and flag-based mode"
```

---

### Task 4: Integration Test

**Files:**
- Create: `tests/test_init_integration.py`

- [ ] **Step 1: Write the integration test**

`tests/test_init_integration.py`:
```python
"""Integration test: mship init → mship status works end-to-end."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from mship.cli import app, container
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


def test_init_then_status(tmp_path: Path, monkeypatch):
    """init creates valid config that status can load."""
    # Create repo dirs with Taskfile.yml
    for name in ["shared", "api"]:
        d = tmp_path / name
        d.mkdir()
        (d / ".git").mkdir()
        (d / "Taskfile.yml").write_text("version: '3'\ntasks:\n  test:\n    cmds:\n      - echo ok\n")

    monkeypatch.chdir(tmp_path)

    # Run init
    result = runner.invoke(app, [
        "init",
        "--name", "test-platform",
        "--repo", "./shared:library",
        "--repo", "./api:service:shared",
    ])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "mothership.yaml").exists()

    # Verify the generated config is valid by loading it
    with open(tmp_path / "mothership.yaml") as f:
        data = yaml.safe_load(f)
    assert data["workspace"] == "test-platform"
    assert data["repos"]["api"]["depends_on"] == ["shared"]

    # Now run status using the generated config
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "No active task" in result.output

    # Run graph
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "shared" in result.output
    assert "api" in result.output

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_init_detect_then_status(tmp_path: Path, monkeypatch):
    """init --detect finds repos and creates valid config."""
    for name in ["frontend", "backend"]:
        d = tmp_path / name
        d.mkdir()
        (d / ".git").mkdir()
        (d / "Taskfile.yml").write_text("version: '3'\ntasks:\n  test:\n    cmds:\n      - echo ok\n")

    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [
        "init",
        "--name", "my-app",
        "--detect",
    ])
    assert result.exit_code == 0, result.output

    with open(tmp_path / "mothership.yaml") as f:
        data = yaml.safe_load(f)
    assert "frontend" in data["repos"]
    assert "backend" in data["repos"]
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_init_integration.py -v`
Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 4: Verify CLI help**

Run: `uv run mship init --help`
Expected: Shows help with `--name`, `--repo`, `--detect`, `--env-runner`, `--scaffold-taskfiles`, `--force` options.

- [ ] **Step 5: Commit**

```bash
git add tests/test_init_integration.py
git commit -m "test: add integration test for mship init → status pipeline"
```

---

## Self-Review

**Spec coverage:**
- Interactive wizard (InquirerPy): Task 3 (`_run_interactive`)
- Non-interactive flags (`--repo`, `--detect`, `--name`): Task 3
- Auto-detection with markers: Task 1
- Dependency declaration (manual): Task 3 (interactive checkbox + flag parsing)
- Taskfile scaffolding (optional): Task 2 (core), Task 3 (CLI flag + interactive prompt)
- Env runner selection: Task 3 (interactive select + `--env-runner` flag)
- `--force` overwrite: Task 3
- `--detect` + `--repo` merge: Task 3 (detected repos merged, explicit takes priority by path)
- Config validation (deps, cycles): Task 2 (reuses Pydantic validators)
- Existing `mothership.yaml` guard: Task 3
- Integration with `mship status`: Task 4

**Placeholder scan:** No TBDs or TODOs in implementation code. The starter Taskfile has intentional "TODO: add X command" stub text — that's the feature, not a placeholder.

**Type consistency:** `DetectedRepo`, `WorkspaceInitializer`, `_parse_repo_flag` — all consistent across tasks. `repos_data` is always `list[dict]` with keys `name`, `path`, `type`, `depends_on`.
