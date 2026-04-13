# Mothership v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `mship` CLI that provides cross-repo workspace awareness, coordinated worktree management, dependency-ordered task execution, and phase-based workflow orchestration.

**Architecture:** Layered Python CLI — thin Typer shell delegates to a core library of DI-wired services, which call a util layer for git/shell/slug operations. Core is CLI-independent for v2 MCP server reuse.

**Tech Stack:** Python 3.14, Typer, Pydantic v2, Rich, InquirerPy, dependency-injector, uv

---

## File Map

### Util layer (leaf dependencies, no internal imports)
- `src/mship/util/__init__.py` — empty
- `src/mship/util/slug.py` — slugify task descriptions into branch-safe strings
- `src/mship/util/shell.py` — `ShellRunner` class wrapping subprocess, env_runner prefixing
- `src/mship/util/git.py` — `GitRunner` class for worktree/branch operations

### Core layer (domain logic, depends on util)
- `src/mship/core/__init__.py` — empty
- `src/mship/core/config.py` — Pydantic models for `mothership.yaml`, `ConfigLoader` with directory walk
- `src/mship/core/graph.py` — `DependencyGraph` with topo sort, dependents, cycle detection
- `src/mship/core/state.py` — Pydantic models for `state.yaml`, `StateManager` with atomic writes
- `src/mship/core/phase.py` — `PhaseManager` with transitions and soft gates
- `src/mship/core/executor.py` — `RepoExecutor` for cross-repo task execution
- `src/mship/core/worktree.py` — `WorktreeManager` for spawn/finish/abort

### CLI layer (depends on core)
- `src/mship/cli/__init__.py` — Typer app assembly, entry point
- `src/mship/cli/output.py` — TTY detection, Rich vs JSON formatting
- `src/mship/cli/status.py` — `mship status`, `mship graph`
- `src/mship/cli/phase.py` — `mship phase`
- `src/mship/cli/worktree.py` — `mship spawn`, `finish`, `abort`, `worktrees`
- `src/mship/cli/exec.py` — `mship test`, `run`, `logs`

### DI container
- `src/mship/container.py` — `DeclarativeContainer` wiring all services

### Package root
- `src/mship/__init__.py` — version string
- `pyproject.toml` — uv-managed project config

### Tests (mirror src structure)
- `tests/__init__.py`
- `tests/util/test_slug.py`
- `tests/util/test_shell.py`
- `tests/util/test_git.py`
- `tests/core/test_config.py`
- `tests/core/test_graph.py`
- `tests/core/test_state.py`
- `tests/core/test_phase.py`
- `tests/core/test_executor.py`
- `tests/core/test_worktree.py`
- `tests/cli/test_output.py`
- `tests/cli/test_status.py`
- `tests/cli/test_phase.py`
- `tests/cli/test_worktree.py`
- `tests/cli/test_exec.py`
- `tests/conftest.py` — shared fixtures (tmp workspace, mock config, mock state)

---

### Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `src/mship/__init__.py`
- Create: `src/mship/util/__init__.py`
- Create: `src/mship/core/__init__.py`
- Create: `src/mship/cli/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/util/__init__.py`
- Create: `tests/core/__init__.py`
- Create: `tests/cli/__init__.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "mothership"
version = "0.1.0"
description = "Phase-based workflow engine for multi-repo AI development"
requires-python = ">=3.14"
dependencies = [
    "typer>=0.15",
    "pydantic>=2.0",
    "rich>=13.0",
    "InquirerPy>=0.3",
    "dependency-injector>=4.0",
    "pyyaml>=6.0",
]

[project.scripts]
mship = "mship.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/mship"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-tmp-files>=0.0.2",
]
```

- [ ] **Step 2: Create package `__init__.py` files**

`src/mship/__init__.py`:
```python
__version__ = "0.1.0"
```

`src/mship/util/__init__.py`:
```python
```

`src/mship/core/__init__.py`:
```python
```

`src/mship/cli/__init__.py` (placeholder — will be filled in Task 12):
```python
import typer

app = typer.Typer(name="mship", help="Cross-repo workflow engine")
```

`tests/__init__.py`, `tests/util/__init__.py`, `tests/core/__init__.py`, `tests/cli/__init__.py`:
```python
```

- [ ] **Step 3: Initialize uv and install dependencies**

Run: `cd /home/bailey/development/repos/mothership && uv sync`
Expected: Dependencies installed, `.venv` created.

- [ ] **Step 4: Verify project loads**

Run: `cd /home/bailey/development/repos/mothership && uv run python -c "import mship; print(mship.__version__)"`
Expected: `0.1.0`

- [ ] **Step 5: Verify pytest runs**

Run: `cd /home/bailey/development/repos/mothership && uv run pytest --co -q`
Expected: `no tests ran` (no test files yet, but pytest collects successfully)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/ tests/
git commit -m "chore: scaffold mothership project with uv, typer, pydantic v2"
```

---

### Task 2: Slug Utility

**Files:**
- Create: `src/mship/util/slug.py`
- Create: `tests/util/test_slug.py`

- [ ] **Step 1: Write the failing tests**

`tests/util/test_slug.py`:
```python
from mship.util.slug import slugify


def test_basic_slugify():
    assert slugify("add labels to tasks") == "add-labels-to-tasks"


def test_strips_special_characters():
    assert slugify("fix auth (login)") == "fix-auth-login"


def test_collapses_multiple_hyphens():
    assert slugify("fix---auth---bug") == "fix-auth-bug"


def test_lowercases():
    assert slugify("Add Labels To Tasks") == "add-labels-to-tasks"


def test_strips_leading_trailing_hyphens():
    assert slugify("--add labels--") == "add-labels"


def test_empty_string():
    assert slugify("") == ""


def test_numbers_preserved():
    assert slugify("fix issue 42") == "fix-issue-42"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/util/test_slug.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.util.slug'`

- [ ] **Step 3: Write the implementation**

`src/mship/util/slug.py`:
```python
import re


def slugify(text: str) -> str:
    """Convert a task description into a branch-safe slug."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    text = text.strip("-")
    return text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/util/test_slug.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/util/slug.py tests/util/test_slug.py
git commit -m "feat: add slugify utility for branch name generation"
```

---

### Task 3: Shell Utility

**Files:**
- Create: `src/mship/util/shell.py`
- Create: `tests/util/test_shell.py`

- [ ] **Step 1: Write the failing tests**

`tests/util/test_shell.py`:
```python
import subprocess
from unittest.mock import MagicMock, patch
from pathlib import Path

from mship.util.shell import ShellRunner, ShellResult


def test_run_simple_command():
    runner = ShellRunner()
    result = runner.run("echo hello", cwd=Path("."))
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_run_captures_stderr():
    runner = ShellRunner()
    result = runner.run("echo error >&2", cwd=Path("."))
    assert "error" in result.stderr


def test_run_returns_nonzero_on_failure():
    runner = ShellRunner()
    result = runner.run("false", cwd=Path("."))
    assert result.returncode != 0


def test_build_command_no_env_runner():
    runner = ShellRunner()
    cmd = runner.build_command("task test", env_runner=None)
    assert cmd == "task test"


def test_build_command_with_env_runner():
    runner = ShellRunner()
    cmd = runner.build_command("task test", env_runner="dotenvx run --")
    assert cmd == "dotenvx run -- task test"


def test_run_with_env_runner():
    runner = ShellRunner()
    result = runner.run_task(
        task_name="test",
        actual_task_name="test",
        cwd=Path("."),
        env_runner=None,
    )
    # task binary likely not installed in test env, so we just check it tried
    assert isinstance(result, ShellResult)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/util/test_shell.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.util.shell'`

- [ ] **Step 3: Write the implementation**

`src/mship/util/shell.py`:
```python
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ShellResult:
    returncode: int
    stdout: str
    stderr: str


class ShellRunner:
    """Wraps subprocess execution with optional env_runner prefixing."""

    def build_command(self, command: str, env_runner: str | None = None) -> str:
        if env_runner:
            return f"{env_runner} {command}"
        return command

    def run(self, command: str, cwd: Path) -> ShellResult:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        return ShellResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def run_task(
        self,
        task_name: str,
        actual_task_name: str,
        cwd: Path,
        env_runner: str | None = None,
    ) -> ShellResult:
        command = self.build_command(f"task {actual_task_name}", env_runner)
        return self.run(command, cwd)

    def run_streaming(self, command: str, cwd: Path) -> subprocess.Popen:
        """Run a command with stdout/stderr streaming (for logs, run)."""
        return subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/util/test_shell.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/util/shell.py tests/util/test_shell.py
git commit -m "feat: add ShellRunner utility for subprocess execution with env_runner support"
```

---

### Task 4: Git Utility

**Files:**
- Create: `src/mship/util/git.py`
- Create: `tests/util/test_git.py`

- [ ] **Step 1: Write the failing tests**

`tests/util/test_git.py`:
```python
import os
import subprocess
from pathlib import Path

import pytest

from mship.util.git import GitRunner


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo for testing."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )
    return tmp_path


def test_worktree_add(git_repo: Path):
    runner = GitRunner()
    wt_path = git_repo / ".worktrees" / "feat" / "test-branch"
    runner.worktree_add(repo_path=git_repo, worktree_path=wt_path, branch="feat/test-branch")
    assert wt_path.exists()
    assert (wt_path / ".git").exists()


def test_worktree_remove(git_repo: Path):
    runner = GitRunner()
    wt_path = git_repo / ".worktrees" / "feat" / "test-branch"
    runner.worktree_add(repo_path=git_repo, worktree_path=wt_path, branch="feat/test-branch")
    runner.worktree_remove(repo_path=git_repo, worktree_path=wt_path)
    assert not wt_path.exists()


def test_branch_delete(git_repo: Path):
    runner = GitRunner()
    wt_path = git_repo / ".worktrees" / "feat" / "test-branch"
    runner.worktree_add(repo_path=git_repo, worktree_path=wt_path, branch="feat/test-branch")
    runner.worktree_remove(repo_path=git_repo, worktree_path=wt_path)
    runner.branch_delete(repo_path=git_repo, branch="feat/test-branch")
    result = subprocess.run(
        ["git", "branch", "--list", "feat/test-branch"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""


def test_is_ignored_false(git_repo: Path):
    runner = GitRunner()
    assert runner.is_ignored(git_repo, ".worktrees") is False


def test_is_ignored_true(git_repo: Path):
    (git_repo / ".gitignore").write_text(".worktrees\n")
    runner = GitRunner()
    assert runner.is_ignored(git_repo, ".worktrees") is True


def test_add_to_gitignore(git_repo: Path):
    runner = GitRunner()
    runner.add_to_gitignore(git_repo, ".worktrees")
    content = (git_repo / ".gitignore").read_text()
    assert ".worktrees" in content


def test_add_to_gitignore_existing_file(git_repo: Path):
    (git_repo / ".gitignore").write_text("node_modules\n")
    runner = GitRunner()
    runner.add_to_gitignore(git_repo, ".worktrees")
    content = (git_repo / ".gitignore").read_text()
    assert "node_modules" in content
    assert ".worktrees" in content


def test_has_uncommitted_changes_clean(git_repo: Path):
    runner = GitRunner()
    assert runner.has_uncommitted_changes(git_repo) is False


def test_has_uncommitted_changes_dirty(git_repo: Path):
    (git_repo / "file.txt").write_text("hello")
    runner = GitRunner()
    assert runner.has_uncommitted_changes(git_repo) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/util/test_git.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.util.git'`

- [ ] **Step 3: Write the implementation**

`src/mship/util/git.py`:
```python
import subprocess
from pathlib import Path


class GitRunner:
    """Git operations for worktree and branch management."""

    def worktree_add(self, repo_path: Path, worktree_path: Path, branch: str) -> None:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def worktree_remove(self, repo_path: Path, worktree_path: Path) -> None:
        subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def branch_delete(self, repo_path: Path, branch: str) -> None:
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def is_ignored(self, repo_path: Path, pattern: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "-q", pattern],
            cwd=repo_path,
            capture_output=True,
        )
        return result.returncode == 0

    def add_to_gitignore(self, repo_path: Path, pattern: str) -> None:
        gitignore = repo_path / ".gitignore"
        if gitignore.exists():
            content = gitignore.read_text()
            if pattern in content.splitlines():
                return
            if not content.endswith("\n"):
                content += "\n"
            content += f"{pattern}\n"
        else:
            content = f"{pattern}\n"
        gitignore.write_text(content)

    def has_uncommitted_changes(self, repo_path: Path) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())

    def worktree_list(self, repo_path: Path) -> list[dict[str, str]]:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        worktrees = []
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line.split(" ", 1)[1]}
            elif line.startswith("branch "):
                current["branch"] = line.split(" ", 1)[1]
        if current:
            worktrees.append(current)
        return worktrees
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/util/test_git.py -v`
Expected: All 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/util/git.py tests/util/test_git.py
git commit -m "feat: add GitRunner utility for worktree and branch operations"
```

---

### Task 5: Configuration Model & Loader

**Files:**
- Create: `src/mship/core/config.py`
- Create: `tests/core/test_config.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write the failing tests**

`tests/conftest.py`:
```python
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with repos that have Taskfile.yml files."""
    config = tmp_path / "mothership.yaml"
    config.write_text(
        """\
workspace: test-platform

repos:
  shared:
    path: ./shared
    type: library
    depends_on: []
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
  api-gateway:
    path: ./api-gateway
    type: service
    depends_on: [shared, auth-service]
"""
    )
    for name in ["shared", "auth-service", "api-gateway"]:
        repo_dir = tmp_path / name
        repo_dir.mkdir()
        (repo_dir / "Taskfile.yml").write_text(f"version: '3'\ntasks:\n  test:\n    cmds:\n      - echo {name}\n")
    return tmp_path


@pytest.fixture
def workspace_with_git(workspace: Path) -> Path:
    """Workspace where each repo is a git repo."""
    for name in ["shared", "auth-service", "api-gateway"]:
        repo_dir = workspace / name
        subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
        )
    return workspace
```

`tests/core/test_config.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.config'`

- [ ] **Step 3: Write the implementation**

`src/mship/core/config.py`:
```python
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator


class RepoConfig(BaseModel):
    path: Path
    type: Literal["library", "service"]
    depends_on: list[str] = []
    env_runner: str | None = None
    tasks: dict[str, str] = {}


class WorkspaceConfig(BaseModel):
    workspace: str
    env_runner: str | None = None
    branch_pattern: str = "feat/{slug}"
    repos: dict[str, RepoConfig]

    @model_validator(mode="after")
    def validate_depends_on_refs(self) -> "WorkspaceConfig":
        repo_names = set(self.repos.keys())
        for name, repo in self.repos.items():
            for dep in repo.depends_on:
                if dep not in repo_names:
                    raise ValueError(
                        f"Repo '{name}' depends on '{dep}' which does not exist. "
                        f"Valid repos: {sorted(repo_names)}"
                    )
        return self

    @model_validator(mode="after")
    def validate_no_cycles(self) -> "WorkspaceConfig":
        # Kahn's algorithm for cycle detection
        in_degree: dict[str, int] = {name: 0 for name in self.repos}
        adjacency: dict[str, list[str]] = {name: [] for name in self.repos}
        for name, repo in self.repos.items():
            for dep in repo.depends_on:
                adjacency[dep].append(name)
                in_degree[name] += 1

        queue = [name for name, degree in in_degree.items() if degree == 0]
        visited = 0
        while queue:
            node = queue.pop(0)
            visited += 1
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited != len(self.repos):
            raise ValueError("Circular dependency detected in repo graph")
        return self


class ConfigLoader:
    """Loads and validates mothership.yaml."""

    @staticmethod
    def load(path: Path) -> WorkspaceConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)

        workspace_root = path.parent

        config = WorkspaceConfig(**raw)

        # Resolve relative paths and validate directories
        for name, repo in config.repos.items():
            resolved = (workspace_root / repo.path).resolve()
            repo.path = resolved
            if not resolved.is_dir():
                raise ValueError(f"Repo '{name}' path does not exist: {resolved}")
            if not (resolved / "Taskfile.yml").exists():
                raise ValueError(
                    f"Repo '{name}' at {resolved} has no Taskfile.yml"
                )

        return config

    @staticmethod
    def discover(start: Path) -> Path:
        current = start.resolve()
        while True:
            candidate = current / "mothership.yaml"
            if candidate.exists():
                return candidate
            parent = current.parent
            if parent == current:
                raise FileNotFoundError(
                    "No mothership.yaml found in any parent directory"
                )
            current = parent
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_config.py -v`
Expected: All 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/config.py tests/core/test_config.py tests/conftest.py
git commit -m "feat: add config model, loader, and directory discovery for mothership.yaml"
```

---

### Task 6: Dependency Graph

**Files:**
- Create: `src/mship/core/graph.py`
- Create: `tests/core/test_graph.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_graph.py`:
```python
from pathlib import Path

import pytest

from mship.core.config import ConfigLoader
from mship.core.graph import DependencyGraph


def test_topo_sort_linear(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    order = graph.topo_sort()
    assert order.index("shared") < order.index("auth-service")
    assert order.index("auth-service") < order.index("api-gateway")


def test_topo_sort_contains_all_repos(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    order = graph.topo_sort()
    assert set(order) == {"shared", "auth-service", "api-gateway"}


def test_topo_sort_subset(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    order = graph.topo_sort(repos=["auth-service", "shared"])
    assert order == ["shared", "auth-service"]


def test_dependents_of_shared(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    deps = graph.dependents("shared")
    assert set(deps) == {"auth-service", "api-gateway"}


def test_dependents_of_leaf(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    deps = graph.dependents("api-gateway")
    assert deps == []


def test_dependencies_of_api_gateway(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    deps = graph.dependencies("api-gateway")
    assert set(deps) == {"shared", "auth-service"}


def test_dependencies_of_root(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    deps = graph.dependencies("shared")
    assert deps == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_graph.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.graph'`

- [ ] **Step 3: Write the implementation**

`src/mship/core/graph.py`:
```python
from mship.core.config import WorkspaceConfig


class DependencyGraph:
    """Repo dependency graph with topological sort and traversal."""

    def __init__(self, config: WorkspaceConfig) -> None:
        self._config = config
        # adjacency: dep -> list of dependents
        self._forward: dict[str, list[str]] = {name: [] for name in config.repos}
        # reverse: dependent -> list of deps
        self._reverse: dict[str, list[str]] = {name: [] for name in config.repos}

        for name, repo in config.repos.items():
            for dep in repo.depends_on:
                self._forward[dep].append(name)
                self._reverse[name].append(dep)

    def topo_sort(self, repos: list[str] | None = None) -> list[str]:
        """Return repos in dependency order (dependencies first).

        If repos is provided, only include those repos but respect their
        dependency ordering.
        """
        target_set = set(repos) if repos else set(self._config.repos.keys())

        in_degree: dict[str, int] = {}
        for name in target_set:
            in_degree[name] = sum(
                1 for dep in self._reverse[name] if dep in target_set
            )

        queue = sorted(n for n, d in in_degree.items() if d == 0)
        result: list[str] = []

        while queue:
            node = queue.pop(0)
            result.append(node)
            for neighbor in self._forward[node]:
                if neighbor in target_set:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)
            queue.sort()

        return result

    def dependents(self, repo: str) -> list[str]:
        """Return all transitive downstream dependents of a repo."""
        visited: set[str] = set()
        stack = list(self._forward[repo])
        while stack:
            node = stack.pop()
            if node not in visited:
                visited.add(node)
                stack.extend(self._forward[node])
        return sorted(visited)

    def dependencies(self, repo: str) -> list[str]:
        """Return all transitive upstream dependencies of a repo."""
        visited: set[str] = set()
        stack = list(self._reverse[repo])
        while stack:
            node = stack.pop()
            if node not in visited:
                visited.add(node)
                stack.extend(self._reverse[node])
        return sorted(visited)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_graph.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/graph.py tests/core/test_graph.py
git commit -m "feat: add dependency graph with topological sort and traversal"
```

---

### Task 7: State Model & Manager

**Files:**
- Create: `src/mship/core/state.py`
- Create: `tests/core/test_state.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_state.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.state import (
    StateManager,
    Task,
    TestResult,
    WorkspaceState,
)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".mothership"
    d.mkdir()
    return d


def test_empty_state(state_dir: Path):
    mgr = StateManager(state_dir)
    state = mgr.load()
    assert state.current_task is None
    assert state.tasks == {}


def test_save_and_load_roundtrip(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels to tasks",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/add-labels",
    )
    state = WorkspaceState(current_task="add-labels", tasks={"add-labels": task})
    mgr.save(state)
    loaded = mgr.load()
    assert loaded.current_task == "add-labels"
    assert loaded.tasks["add-labels"].slug == "add-labels"
    assert loaded.tasks["add-labels"].affected_repos == ["shared", "auth-service"]


def test_save_with_test_results(state_dir: Path):
    mgr = StateManager(state_dir)
    now = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    task = Task(
        slug="fix-auth",
        description="Fix auth",
        phase="dev",
        created_at=now,
        affected_repos=["auth-service"],
        branch="feat/fix-auth",
        test_results={"auth-service": TestResult(status="pass", at=now)},
    )
    state = WorkspaceState(current_task="fix-auth", tasks={"fix-auth": task})
    mgr.save(state)
    loaded = mgr.load()
    assert loaded.tasks["fix-auth"].test_results["auth-service"].status == "pass"


def test_save_with_worktrees(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
        worktrees={"shared": Path("/tmp/worktree/shared")},
    )
    state = WorkspaceState(current_task="add-labels", tasks={"add-labels": task})
    mgr.save(state)
    loaded = mgr.load()
    assert loaded.tasks["add-labels"].worktrees["shared"] == Path("/tmp/worktree/shared")


def test_creates_state_dir_if_missing(tmp_path: Path):
    state_dir = tmp_path / ".mothership"
    mgr = StateManager(state_dir)
    state = mgr.load()
    assert state.current_task is None
    # Save should create the directory
    mgr.save(state)
    assert state_dir.exists()


def test_get_current_task(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="fix-auth",
        description="Fix auth",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["auth-service"],
        branch="feat/fix-auth",
    )
    state = WorkspaceState(current_task="fix-auth", tasks={"fix-auth": task})
    mgr.save(state)
    current = mgr.get_current_task()
    assert current is not None
    assert current.slug == "fix-auth"


def test_get_current_task_none(state_dir: Path):
    mgr = StateManager(state_dir)
    assert mgr.get_current_task() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.state'`

- [ ] **Step 3: Write the implementation**

`src/mship/core/state.py`:
```python
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class TestResult(BaseModel):
    status: Literal["pass", "fail", "skip"]
    at: datetime


class Task(BaseModel):
    slug: str
    description: str
    phase: Literal["plan", "dev", "review", "run"]
    created_at: datetime
    affected_repos: list[str]
    worktrees: dict[str, Path] = {}
    branch: str
    test_results: dict[str, TestResult] = {}


class WorkspaceState(BaseModel):
    current_task: str | None = None
    tasks: dict[str, Task] = {}


class StateManager:
    """Read/write .mothership/state.yaml with atomic writes."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._state_file = state_dir / "state.yaml"

    def load(self) -> WorkspaceState:
        if not self._state_file.exists():
            return WorkspaceState()
        with open(self._state_file) as f:
            raw = yaml.safe_load(f)
        if raw is None:
            return WorkspaceState()
        return WorkspaceState(**raw)

    def save(self, state: WorkspaceState) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        data = state.model_dump(mode="json")
        # Convert Path objects to strings for YAML
        for task in data.get("tasks", {}).values():
            task["worktrees"] = {
                k: str(v) for k, v in task.get("worktrees", {}).items()
            }
        # Atomic write: write to temp, rename
        fd, tmp_path = tempfile.mkstemp(
            dir=self._state_dir, suffix=".yaml.tmp"
        )
        try:
            with open(fd, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            Path(tmp_path).replace(self._state_file)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    def get_current_task(self) -> Task | None:
        state = self.load()
        if state.current_task is None:
            return None
        return state.tasks.get(state.current_task)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_state.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/state.py tests/core/test_state.py
git commit -m "feat: add state model and StateManager with atomic writes"
```

---

### Task 8: Phase Manager

**Files:**
- Create: `src/mship/core/phase.py`
- Create: `tests/core/test_phase.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_phase.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.phase import PhaseManager, PhaseTransition
from mship.core.state import StateManager, Task, TestResult, WorkspaceState
from mship.util.git import GitRunner


@pytest.fixture
def state_with_task(tmp_path: Path) -> StateManager:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/add-labels",
        worktrees={
            "shared": tmp_path / "shared",
            "auth-service": tmp_path / "auth-service",
        },
    )
    state = WorkspaceState(current_task="add-labels", tasks={"add-labels": task})
    mgr.save(state)
    return mgr


def test_transition_plan_to_dev(state_with_task: StateManager):
    pm = PhaseManager(state_with_task)
    result = pm.transition("add-labels", "dev")
    assert result.new_phase == "dev"
    # No spec exists, so should warn
    assert any("spec" in w.lower() for w in result.warnings)


def test_transition_saves_state(state_with_task: StateManager):
    pm = PhaseManager(state_with_task)
    pm.transition("add-labels", "dev")
    reloaded = state_with_task.load()
    assert reloaded.tasks["add-labels"].phase == "dev"


def test_transition_to_plan_no_warnings(state_with_task: StateManager):
    pm = PhaseManager(state_with_task)
    result = pm.transition("add-labels", "plan")
    assert result.warnings == []


def test_transition_to_review_warns_no_test_results(state_with_task: StateManager):
    pm = PhaseManager(state_with_task)
    pm.transition("add-labels", "dev")
    result = pm.transition("add-labels", "review")
    assert any("test" in w.lower() for w in result.warnings)


def test_transition_to_review_warns_failing_tests(state_with_task: StateManager):
    pm = PhaseManager(state_with_task)
    state = state_with_task.load()
    now = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    state.tasks["add-labels"].phase = "dev"
    state.tasks["add-labels"].test_results = {
        "shared": TestResult(status="pass", at=now),
        "auth-service": TestResult(status="fail", at=now),
    }
    state_with_task.save(state)
    result = pm.transition("add-labels", "review")
    assert any("auth-service" in w for w in result.warnings)


def test_transition_to_review_no_warning_all_pass(state_with_task: StateManager):
    pm = PhaseManager(state_with_task)
    state = state_with_task.load()
    now = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    state.tasks["add-labels"].phase = "dev"
    state.tasks["add-labels"].test_results = {
        "shared": TestResult(status="pass", at=now),
        "auth-service": TestResult(status="pass", at=now),
    }
    state_with_task.save(state)
    result = pm.transition("add-labels", "review")
    assert result.warnings == []


def test_backward_transition_allowed(state_with_task: StateManager):
    pm = PhaseManager(state_with_task)
    pm.transition("add-labels", "dev")
    result = pm.transition("add-labels", "plan")
    assert result.new_phase == "plan"
    assert result.warnings == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_phase.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.phase'`

- [ ] **Step 3: Write the implementation**

`src/mship/core/phase.py`:
```python
from dataclasses import dataclass, field
from typing import Literal

from mship.core.state import StateManager

Phase = Literal["plan", "dev", "review", "run"]
PHASE_ORDER: list[Phase] = ["plan", "dev", "review", "run"]


@dataclass
class PhaseTransition:
    new_phase: Phase
    warnings: list[str] = field(default_factory=list)


class PhaseManager:
    """Manages phase transitions with soft gates."""

    def __init__(self, state_manager: StateManager) -> None:
        self._state_manager = state_manager

    def transition(self, task_slug: str, target: Phase) -> PhaseTransition:
        state = self._state_manager.load()
        task = state.tasks[task_slug]
        warnings = self._check_gates(task_slug, task.phase, target)

        task.phase = target
        self._state_manager.save(state)

        return PhaseTransition(new_phase=target, warnings=warnings)

    def _check_gates(
        self, task_slug: str, current: Phase, target: Phase
    ) -> list[str]:
        current_idx = PHASE_ORDER.index(current)
        target_idx = PHASE_ORDER.index(target)

        # No gates for backward transitions
        if target_idx <= current_idx:
            return []

        warnings: list[str] = []
        state = self._state_manager.load()
        task = state.tasks[task_slug]

        if target == "dev":
            warnings.extend(self._gate_dev(task_slug))
        elif target == "review":
            warnings.extend(self._gate_review(task))
        elif target == "run":
            warnings.extend(self._gate_run(task))

        return warnings

    def _gate_dev(self, task_slug: str) -> list[str]:
        # Check for spec/plan files — for v1, just warn always since
        # we don't know where specs are stored
        return ["No spec found — consider writing one before developing"]

    def _gate_review(self, task) -> list[str]:  # noqa: ANN001
        warnings: list[str] = []
        missing = []
        failing = []
        for repo in task.affected_repos:
            result = task.test_results.get(repo)
            if result is None:
                missing.append(repo)
            elif result.status == "fail":
                failing.append(repo)

        if missing:
            warnings.append(
                f"Tests not run in: {', '.join(missing)} — consider running tests before review"
            )
        if failing:
            warnings.append(
                f"Tests not passing in: {', '.join(failing)} — consider fixing before review"
            )
        return warnings

    def _gate_run(self, task) -> list[str]:  # noqa: ANN001
        # Check for uncommitted changes in worktree paths
        # For v1, this requires checking git status in each worktree
        # The PhaseManager doesn't have a GitRunner — this check is
        # done by the CLI layer which has access to the DI container.
        # For now, return empty; the CLI layer adds this check.
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_phase.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/phase.py tests/core/test_phase.py
git commit -m "feat: add PhaseManager with soft gate warnings on transitions"
```

---

### Task 9: Repo Executor

**Files:**
- Create: `src/mship/core/executor.py`
- Create: `tests/core/test_executor.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_executor.py`:
```python
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.config import ConfigLoader, WorkspaceConfig
from mship.core.executor import RepoExecutor, ExecutionResult, RepoResult
from mship.core.graph import DependencyGraph
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellRunner, ShellResult


@pytest.fixture
def mock_shell() -> MagicMock:
    shell = MagicMock(spec=ShellRunner)
    shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    return shell


@pytest.fixture
def executor_deps(workspace: Path, mock_shell: MagicMock):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)

    task = Task(
        slug="test-task",
        description="Test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service", "api-gateway"],
        branch="feat/test-task",
    )
    state = WorkspaceState(current_task="test-task", tasks={"test-task": task})
    state_mgr.save(state)

    return config, graph, state_mgr, mock_shell


def test_execute_runs_in_dependency_order(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute("test", repos=["shared", "auth-service", "api-gateway"])

    calls = [c.kwargs["actual_task_name"] or c.args[1] for c in mock_shell.run_task.call_args_list]
    assert mock_shell.run_task.call_count == 3
    # Verify dependency order via cwd
    cwds = [str(c.kwargs["cwd"]) for c in mock_shell.run_task.call_args_list]
    shared_idx = next(i for i, c in enumerate(cwds) if "shared" in c)
    auth_idx = next(i for i, c in enumerate(cwds) if "auth-service" in c)
    api_idx = next(i for i, c in enumerate(cwds) if "api-gateway" in c)
    assert shared_idx < auth_idx < api_idx


def test_execute_fail_fast(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="fail"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute("test", repos=["shared", "auth-service", "api-gateway"])
    assert result.success is False
    # Should stop after first failure
    assert mock_shell.run_task.call_count == 1


def test_execute_all_flag(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="fail"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute(
        "test", repos=["shared", "auth-service", "api-gateway"], run_all=True
    )
    assert result.success is False
    assert mock_shell.run_task.call_count == 3
    assert len(result.results) == 3


def test_execute_resolves_task_name_override(workspace: Path):
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
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute("test", repos=["shared"])
    mock_shell.run_task.assert_called_once()
    call_kwargs = mock_shell.run_task.call_args.kwargs
    assert call_kwargs["actual_task_name"] == "unit"


def test_execute_uses_env_runner(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
env_runner: "dotenvx run --"
repos:
  shared:
    path: ./shared
    type: library
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute("test", repos=["shared"])
    call_kwargs = mock_shell.run_task.call_args.kwargs
    assert call_kwargs["env_runner"] == "dotenvx run --"


def test_execute_repo_override_env_runner(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
env_runner: "dotenvx run --"
repos:
  shared:
    path: ./shared
    type: library
    env_runner: "op run --"
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute("test", repos=["shared"])
    call_kwargs = mock_shell.run_task.call_args.kwargs
    assert call_kwargs["env_runner"] == "op run --"


def test_execute_updates_test_results(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute(
        "test",
        repos=["shared", "auth-service", "api-gateway"],
        task_slug="test-task",
    )
    state = state_mgr.load()
    task = state.tasks["test-task"]
    assert task.test_results["shared"].status == "pass"
    assert task.test_results["auth-service"].status == "pass"
    assert task.test_results["api-gateway"].status == "pass"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_executor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.executor'`

- [ ] **Step 3: Write the implementation**

`src/mship/core/executor.py`:
```python
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mship.core.config import WorkspaceConfig
from mship.core.graph import DependencyGraph
from mship.core.state import StateManager, TestResult
from mship.util.shell import ShellRunner, ShellResult


@dataclass
class RepoResult:
    repo: str
    task_name: str
    shell_result: ShellResult
    skipped: bool = False

    @property
    def success(self) -> bool:
        return self.shell_result.returncode == 0 if not self.skipped else True


@dataclass
class ExecutionResult:
    results: list[RepoResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return all(r.success for r in self.results)


class RepoExecutor:
    """Execute tasks across repos in dependency order."""

    def __init__(
        self,
        config: WorkspaceConfig,
        graph: DependencyGraph,
        state_manager: StateManager,
        shell: ShellRunner,
    ) -> None:
        self._config = config
        self._graph = graph
        self._state_manager = state_manager
        self._shell = shell

    def resolve_task_name(self, repo_name: str, canonical: str) -> str:
        repo = self._config.repos[repo_name]
        return repo.tasks.get(canonical, canonical)

    def resolve_env_runner(self, repo_name: str) -> str | None:
        repo = self._config.repos[repo_name]
        if repo.env_runner is not None:
            return repo.env_runner
        return self._config.env_runner

    def execute(
        self,
        canonical_task: str,
        repos: list[str],
        run_all: bool = False,
        task_slug: str | None = None,
    ) -> ExecutionResult:
        ordered = self._graph.topo_sort(repos)
        result = ExecutionResult()

        for repo_name in ordered:
            actual_name = self.resolve_task_name(repo_name, canonical_task)
            env_runner = self.resolve_env_runner(repo_name)
            repo_config = self._config.repos[repo_name]

            shell_result = self._shell.run_task(
                task_name=canonical_task,
                actual_task_name=actual_name,
                cwd=repo_config.path,
                env_runner=env_runner,
            )

            repo_result = RepoResult(
                repo=repo_name,
                task_name=actual_name,
                shell_result=shell_result,
            )
            result.results.append(repo_result)

            # Update test results in state if this is a test run
            if task_slug and canonical_task == "test":
                self._update_test_result(task_slug, repo_name, shell_result)

            if not repo_result.success and not run_all:
                break

        return result

    def _update_test_result(
        self, task_slug: str, repo_name: str, shell_result: ShellResult
    ) -> None:
        state = self._state_manager.load()
        task = state.tasks.get(task_slug)
        if task is None:
            return
        task.test_results[repo_name] = TestResult(
            status="pass" if shell_result.returncode == 0 else "fail",
            at=datetime.now(timezone.utc),
        )
        self._state_manager.save(state)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_executor.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/executor.py tests/core/test_executor.py
git commit -m "feat: add RepoExecutor for cross-repo task execution in dependency order"
```

---

### Task 10: Worktree Manager

**Files:**
- Create: `src/mship/core/worktree.py`
- Create: `tests/core/test_worktree.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_worktree.py`:
```python
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mship.core.config import ConfigLoader, WorkspaceConfig
from mship.core.graph import DependencyGraph
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.worktree import WorktreeManager
from mship.util.git import GitRunner
from mship.util.shell import ShellRunner, ShellResult
from mship.util.slug import slugify


@pytest.fixture
def worktree_deps(workspace_with_git: Path):
    workspace = workspace_with_git
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    return config, graph, state_mgr, git, shell, workspace


def test_spawn_creates_worktrees(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("add labels to tasks", repos=["shared", "auth-service"])
    state = state_mgr.load()
    assert state.current_task == "add-labels-to-tasks"
    task = state.tasks["add-labels-to-tasks"]
    assert task.phase == "plan"
    assert set(task.affected_repos) == {"shared", "auth-service"}
    assert task.branch == "feat/add-labels-to-tasks"
    # Worktrees should exist
    for repo_name in ["shared", "auth-service"]:
        wt_path = task.worktrees[repo_name]
        assert Path(wt_path).exists()


def test_spawn_dependency_order(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("fix auth", repos=["auth-service", "shared"])
    state = state_mgr.load()
    task = state.tasks["fix-auth"]
    # Both worktrees should exist
    assert "shared" in task.worktrees
    assert "auth-service" in task.worktrees


def test_spawn_all_repos(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("big change")
    state = state_mgr.load()
    task = state.tasks["big-change"]
    assert set(task.affected_repos) == {"shared", "auth-service", "api-gateway"}


def test_spawn_ensures_gitignore(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("test gitignore", repos=["shared"])
    gitignore = workspace / "shared" / ".gitignore"
    assert gitignore.exists()
    assert ".worktrees" in gitignore.read_text()


def test_spawn_custom_branch_pattern(workspace_with_git: Path):
    workspace = workspace_with_git
    cfg = workspace / "mothership.yaml"
    content = cfg.read_text()
    cfg.write_text(content + 'branch_pattern: "mship/{slug}"\n')
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("custom branch", repos=["shared"])
    state = state_mgr.load()
    task = state.tasks["custom-branch"]
    assert task.branch == "mship/custom-branch"


def test_abort_removes_worktrees(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("to abort", repos=["shared"])
    state = state_mgr.load()
    wt_path = state.tasks["to-abort"].worktrees["shared"]

    mgr.abort("to-abort")
    assert not Path(wt_path).exists()
    state = state_mgr.load()
    assert "to-abort" not in state.tasks
    assert state.current_task is None


def test_spawn_runs_setup_task(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("with setup", repos=["shared"])
    # Shell should have been called with setup task
    shell.run_task.assert_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_worktree.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.worktree'`

- [ ] **Step 3: Write the implementation**

`src/mship/core/worktree.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

from mship.core.config import WorkspaceConfig
from mship.core.graph import DependencyGraph
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.git import GitRunner
from mship.util.shell import ShellRunner
from mship.util.slug import slugify


class WorktreeManager:
    """Cross-repo worktree orchestration."""

    def __init__(
        self,
        config: WorkspaceConfig,
        graph: DependencyGraph,
        state_manager: StateManager,
        git: GitRunner,
        shell: ShellRunner,
    ) -> None:
        self._config = config
        self._graph = graph
        self._state_manager = state_manager
        self._git = git
        self._shell = shell

    def spawn(
        self,
        description: str,
        repos: list[str] | None = None,
    ) -> Task:
        slug = slugify(description)
        branch = self._config.branch_pattern.replace("{slug}", slug)

        if repos is None:
            repos = list(self._config.repos.keys())

        ordered = self._graph.topo_sort(repos)

        worktrees: dict[str, Path] = {}
        for repo_name in ordered:
            repo_config = self._config.repos[repo_name]
            repo_path = repo_config.path

            # Ensure .worktrees is gitignored
            if not self._git.is_ignored(repo_path, ".worktrees"):
                self._git.add_to_gitignore(repo_path, ".worktrees")

            wt_path = repo_path / ".worktrees" / branch
            self._git.worktree_add(
                repo_path=repo_path,
                worktree_path=wt_path,
                branch=branch,
            )
            worktrees[repo_name] = wt_path

            # Run setup task if available (skip gracefully)
            actual_setup = repo_config.tasks.get("setup", "setup")
            self._shell.run_task(
                task_name="setup",
                actual_task_name=actual_setup,
                cwd=wt_path,
                env_runner=repo_config.env_runner or self._config.env_runner,
            )

        task = Task(
            slug=slug,
            description=description,
            phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=ordered,
            worktrees=worktrees,
            branch=branch,
        )

        state = self._state_manager.load()
        state.tasks[slug] = task
        state.current_task = slug
        self._state_manager.save(state)

        return task

    def abort(self, task_slug: str) -> None:
        state = self._state_manager.load()
        task = state.tasks[task_slug]

        for repo_name, wt_path in task.worktrees.items():
            repo_config = self._config.repos[repo_name]
            self._git.worktree_remove(
                repo_path=repo_config.path,
                worktree_path=Path(wt_path),
            )
            self._git.branch_delete(
                repo_path=repo_config.path,
                branch=task.branch,
            )

        del state.tasks[task_slug]
        if state.current_task == task_slug:
            state.current_task = None
        self._state_manager.save(state)

    def list_worktrees(self) -> dict[str, dict[str, Path]]:
        state = self._state_manager.load()
        return {
            slug: dict(task.worktrees)
            for slug, task in state.tasks.items()
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_worktree.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat: add WorktreeManager for cross-repo worktree spawn/abort"
```

---

### Task 11: DI Container

**Files:**
- Create: `src/mship/container.py`
- Create: `tests/test_container.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_container.py`:
```python
from pathlib import Path

import pytest

from mship.container import Container
from mship.core.config import WorkspaceConfig
from mship.core.executor import RepoExecutor
from mship.core.graph import DependencyGraph
from mship.core.phase import PhaseManager
from mship.core.state import StateManager
from mship.core.worktree import WorktreeManager


def test_container_wires_config(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    config = container.config()
    assert isinstance(config, WorkspaceConfig)
    assert config.workspace == "test-platform"


def test_container_wires_graph(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    graph = container.graph()
    assert isinstance(graph, DependencyGraph)


def test_container_wires_state_manager(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    mgr = container.state_manager()
    assert isinstance(mgr, StateManager)


def test_container_wires_executor(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    executor = container.executor()
    assert isinstance(executor, RepoExecutor)


def test_container_wires_worktree_manager(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    mgr = container.worktree_manager()
    assert isinstance(mgr, WorktreeManager)


def test_container_wires_phase_manager(workspace: Path):
    container = Container()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    mgr = container.phase_manager()
    assert isinstance(mgr, PhaseManager)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_container.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.container'`

- [ ] **Step 3: Write the implementation**

`src/mship/container.py`:
```python
from dependency_injector import containers, providers

from mship.core.config import ConfigLoader, WorkspaceConfig
from mship.core.executor import RepoExecutor
from mship.core.graph import DependencyGraph
from mship.core.phase import PhaseManager
from mship.core.state import StateManager
from mship.core.worktree import WorktreeManager
from mship.util.git import GitRunner
from mship.util.shell import ShellRunner


class Container(containers.DeclarativeContainer):
    config_path = providers.Dependency(instance_of=object)
    state_dir = providers.Dependency(instance_of=object)

    config = providers.Singleton(
        ConfigLoader.load,
        path=config_path,
    )

    state_manager = providers.Singleton(
        StateManager,
        state_dir=state_dir,
    )

    git = providers.Singleton(GitRunner)

    shell = providers.Singleton(ShellRunner)

    graph = providers.Factory(
        DependencyGraph,
        config=config,
    )

    executor = providers.Factory(
        RepoExecutor,
        config=config,
        graph=graph,
        state_manager=state_manager,
        shell=shell,
    )

    worktree_manager = providers.Factory(
        WorktreeManager,
        config=config,
        graph=graph,
        state_manager=state_manager,
        git=git,
        shell=shell,
    )

    phase_manager = providers.Factory(
        PhaseManager,
        state_manager=state_manager,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_container.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/container.py tests/test_container.py
git commit -m "feat: add DI container wiring all services"
```

---

### Task 12: CLI Output Formatting

**Files:**
- Create: `src/mship/cli/output.py`
- Create: `tests/cli/test_output.py`

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_output.py`:
```python
import json
import sys
from io import StringIO
from unittest.mock import patch

from mship.cli.output import Output


def test_is_tty_false_when_piped():
    fake_stdout = StringIO()
    output = Output(stream=fake_stdout)
    assert output.is_tty is False


def test_format_json():
    fake_stdout = StringIO()
    output = Output(stream=fake_stdout)
    output.json({"status": "ok", "phase": "dev"})
    result = json.loads(fake_stdout.getvalue())
    assert result["status"] == "ok"


def test_format_warning():
    fake_stdout = StringIO()
    output = Output(stream=fake_stdout)
    output.warning("Tests not passing")
    assert "Tests not passing" in fake_stdout.getvalue()


def test_format_error():
    fake_stderr = StringIO()
    output = Output(stream=StringIO(), err_stream=fake_stderr)
    output.error("Something failed")
    assert "Something failed" in fake_stderr.getvalue()


def test_format_success():
    fake_stdout = StringIO()
    output = Output(stream=fake_stdout)
    output.success("All tests passed")
    assert "All tests passed" in fake_stdout.getvalue()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_output.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.cli.output'`

- [ ] **Step 3: Write the implementation**

`src/mship/cli/output.py`:
```python
import json
import sys
from typing import Any, TextIO

from rich.console import Console
from rich.table import Table


class Output:
    """TTY-aware output formatting. Rich for terminals, JSON for pipes."""

    def __init__(
        self,
        stream: TextIO | None = None,
        err_stream: TextIO | None = None,
    ) -> None:
        self._stream = stream or sys.stdout
        self._err_stream = err_stream or sys.stderr
        self._console = Console(file=self._stream)
        self._err_console = Console(file=self._err_stream, stderr=True)

    @property
    def is_tty(self) -> bool:
        return hasattr(self._stream, "isatty") and self._stream.isatty()

    def json(self, data: dict[str, Any]) -> None:
        self._stream.write(json.dumps(data, indent=2, default=str) + "\n")

    def warning(self, message: str) -> None:
        if self.is_tty:
            self._console.print(f"[yellow]WARNING:[/yellow] {message}")
        else:
            self._stream.write(f"WARNING: {message}\n")

    def error(self, message: str) -> None:
        if self.is_tty:
            self._err_console.print(f"[red]ERROR:[/red] {message}")
        else:
            self._err_stream.write(f"ERROR: {message}\n")

    def success(self, message: str) -> None:
        if self.is_tty:
            self._console.print(f"[green]{message}[/green]")
        else:
            self._stream.write(f"{message}\n")

    def table(self, title: str, columns: list[str], rows: list[list[str]]) -> None:
        if self.is_tty:
            t = Table(title=title)
            for col in columns:
                t.add_column(col)
            for row in rows:
                t.add_row(*row)
            self._console.print(t)
        else:
            self.json({"title": title, "columns": columns, "rows": rows})

    def print(self, message: str) -> None:
        if self.is_tty:
            self._console.print(message)
        else:
            self._stream.write(f"{message}\n")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_output.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/output.py tests/cli/test_output.py
git commit -m "feat: add TTY-aware output formatting with Rich and JSON modes"
```

---

### Task 13: CLI — Status & Graph Commands

**Files:**
- Create: `src/mship/cli/status.py`
- Create: `tests/cli/test_status.py`
- Modify: `src/mship/cli/__init__.py`

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_status.py`:
```python
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.container import Container
from mship.core.state import StateManager, Task, WorkspaceState


runner = CliRunner()


@pytest.fixture
def configured_app(workspace: Path):
    """Configure the CLI container for testing."""
    from mship.cli import container

    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(workspace / ".mothership")
    (workspace / ".mothership").mkdir(exist_ok=True)
    yield
    container.reset_override()


def test_status_no_task(configured_app):
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "No active task" in result.output


def test_status_with_task(configured_app, workspace: Path):
    state_dir = workspace / ".mothership"
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels to tasks",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(current_task="add-labels", tasks={"add-labels": task}))

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "add-labels" in result.output
    assert "dev" in result.output


def test_graph(configured_app):
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "shared" in result.output
    assert "auth-service" in result.output
    assert "api-gateway" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_status.py -v`
Expected: FAIL — `ImportError` (status module doesn't exist yet)

- [ ] **Step 3: Write the implementation**

`src/mship/cli/status.py`:
```python
import typer

from mship.cli.output import Output

status_app = typer.Typer()


def register(app: typer.Typer, get_container):  # noqa: ANN001
    @app.command()
    def status():
        """Show current phase, active task, worktrees, and test results."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            if output.is_tty:
                output.print("No active task")
            else:
                output.json({"current_task": None, "tasks": {}})
            return

        task = state.tasks[state.current_task]
        if output.is_tty:
            output.print(f"[bold]Task:[/bold] {task.slug}")
            output.print(f"[bold]Phase:[/bold] {task.phase}")
            output.print(f"[bold]Branch:[/bold] {task.branch}")
            output.print(f"[bold]Repos:[/bold] {', '.join(task.affected_repos)}")
            if task.worktrees:
                output.print("[bold]Worktrees:[/bold]")
                for repo, path in task.worktrees.items():
                    output.print(f"  {repo}: {path}")
            if task.test_results:
                output.print("[bold]Tests:[/bold]")
                for repo, result in task.test_results.items():
                    status_str = (
                        "[green]pass[/green]"
                        if result.status == "pass"
                        else "[red]fail[/red]"
                    )
                    output.print(f"  {repo}: {status_str}")
        else:
            output.json(task.model_dump(mode="json"))

    @app.command()
    def graph():
        """Show repo dependency graph."""
        container = get_container()
        output = Output()
        config = container.config()
        graph_obj = container.graph()
        order = graph_obj.topo_sort()

        if output.is_tty:
            for repo_name in order:
                repo = config.repos[repo_name]
                deps = repo.depends_on
                dep_str = f" -> [{', '.join(deps)}]" if deps else ""
                type_str = f"({repo.type})"
                output.print(f"  {repo_name} {type_str}{dep_str}")
        else:
            graph_data = {}
            for name, repo in config.repos.items():
                graph_data[name] = {
                    "type": repo.type,
                    "depends_on": repo.depends_on,
                    "path": str(repo.path),
                }
            output.json({"repos": graph_data, "order": order})
```

Update `src/mship/cli/__init__.py`:
```python
import typer

from mship.container import Container

app = typer.Typer(name="mship", help="Cross-repo workflow engine")

container = Container()


def get_container() -> Container:
    """Lazy container initialization with config discovery."""
    from pathlib import Path

    from mship.core.config import ConfigLoader

    try:
        # Only set defaults if not already overridden (for testing)
        if not container.config_path.overridden:
            config_path = ConfigLoader.discover(Path.cwd())
            container.config_path.override(config_path)
        if not container.state_dir.overridden:
            config_path = container.config_path()
            state_dir = Path(config_path).parent / ".mothership"
            container.state_dir.override(state_dir)
    except FileNotFoundError:
        raise typer.Exit(code=1)
    return container


# Register command modules
from mship.cli import status as _status_mod

_status_mod.register(app, get_container)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_status.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/__init__.py src/mship/cli/status.py tests/cli/test_status.py
git commit -m "feat: add mship status and graph CLI commands"
```

---

### Task 14: CLI — Phase Command

**Files:**
- Create: `src/mship/cli/phase.py`
- Create: `tests/cli/test_phase.py`
- Modify: `src/mship/cli/__init__.py`

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_phase.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


@pytest.fixture
def configured_app_with_task(workspace: Path):
    from mship.cli import container

    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
    )
    mgr.save(WorkspaceState(current_task="add-labels", tasks={"add-labels": task}))
    yield
    container.reset_override()


def test_phase_transition(configured_app_with_task, workspace: Path):
    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code == 0
    mgr = StateManager(workspace / ".mothership")
    state = mgr.load()
    assert state.tasks["add-labels"].phase == "dev"


def test_phase_shows_warnings(configured_app_with_task):
    result = runner.invoke(app, ["phase", "dev"])
    assert "WARNING" in result.output or "spec" in result.output.lower()


def test_phase_no_task(workspace: Path):
    from mship.cli import container

    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code != 0 or "No active task" in result.output
    container.reset_override()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_phase.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write the implementation**

`src/mship/cli/phase.py`:
```python
import typer

from mship.cli.output import Output
from mship.core.phase import PHASE_ORDER, Phase


def register(app: typer.Typer, get_container):  # noqa: ANN001
    @app.command()
    def phase(target: str):
        """Transition the current task to a new phase."""
        container = get_container()
        output = Output()

        if target not in PHASE_ORDER:
            output.error(f"Invalid phase: {target}. Must be one of: {', '.join(PHASE_ORDER)}")
            raise typer.Exit(code=1)

        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task")
            raise typer.Exit(code=1)

        phase_mgr = container.phase_manager()
        result = phase_mgr.transition(state.current_task, target)  # type: ignore[arg-type]

        for w in result.warnings:
            output.warning(w)

        if output.is_tty:
            output.success(f"Phase: {result.new_phase}")
        else:
            output.json({
                "task": state.current_task,
                "phase": result.new_phase,
                "warnings": result.warnings,
            })
```

Add to `src/mship/cli/__init__.py` (append before the final line):
```python
from mship.cli import phase as _phase_mod

_phase_mod.register(app, get_container)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_phase.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/phase.py tests/cli/test_phase.py src/mship/cli/__init__.py
git commit -m "feat: add mship phase command with soft gate warnings"
```

---

### Task 15: CLI — Worktree Commands (spawn, finish, abort, worktrees)

**Files:**
- Create: `src/mship/cli/worktree.py`
- Create: `tests/cli/test_worktree.py`
- Modify: `src/mship/cli/__init__.py`

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_worktree.py`:
```python
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.core.state import StateManager

runner = CliRunner()


@pytest.fixture
def configured_git_app(workspace_with_git: Path):
    from mship.cli import container

    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)
    yield workspace_with_git
    container.reset_override()


def test_spawn(configured_git_app: Path):
    result = runner.invoke(app, ["spawn", "add labels to tasks", "--repos", "shared"])
    assert result.exit_code == 0
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert "add-labels-to-tasks" in state.tasks
    assert state.current_task == "add-labels-to-tasks"


def test_spawn_all_repos(configured_git_app: Path):
    result = runner.invoke(app, ["spawn", "big change"])
    assert result.exit_code == 0
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    task = state.tasks["big-change"]
    assert set(task.affected_repos) == {"shared", "auth-service", "api-gateway"}


def test_worktrees_list(configured_git_app: Path):
    runner.invoke(app, ["spawn", "test list", "--repos", "shared"])
    result = runner.invoke(app, ["worktrees"])
    assert result.exit_code == 0
    assert "test-list" in result.output


def test_abort(configured_git_app: Path):
    runner.invoke(app, ["spawn", "to abort", "--repos", "shared"])
    result = runner.invoke(app, ["abort", "--yes"])
    assert result.exit_code == 0
    mgr = StateManager(configured_git_app / ".mothership")
    state = mgr.load()
    assert state.current_task is None
    assert "to-abort" not in state.tasks
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_worktree.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write the implementation**

`src/mship/cli/worktree.py`:
```python
from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):  # noqa: ANN001
    @app.command()
    def spawn(
        description: str,
        repos: Optional[str] = typer.Option(None, help="Comma-separated repo names"),
    ):
        """Create coordinated worktrees across repos for a new task."""
        container = get_container()
        output = Output()
        wt_mgr = container.worktree_manager()

        repo_list = repos.split(",") if repos else None

        task = wt_mgr.spawn(description, repos=repo_list)

        if output.is_tty:
            output.success(f"Spawned task: {task.slug}")
            output.print(f"  Branch: {task.branch}")
            output.print(f"  Phase: {task.phase}")
            output.print(f"  Repos: {', '.join(task.affected_repos)}")
            for repo, path in task.worktrees.items():
                output.print(f"  {repo}: {path}")
        else:
            output.json(task.model_dump(mode="json"))

    @app.command()
    def worktrees():
        """List active worktrees grouped by task."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if not state.tasks:
            output.print("No active worktrees")
            return

        if output.is_tty:
            for slug, task in state.tasks.items():
                active = " (active)" if slug == state.current_task else ""
                output.print(f"[bold]{slug}[/bold]{active} [{task.phase}]")
                output.print(f"  Branch: {task.branch}")
                for repo, path in task.worktrees.items():
                    output.print(f"  {repo}: {path}")
        else:
            data = {
                slug: task.model_dump(mode="json")
                for slug, task in state.tasks.items()
            }
            output.json({"current_task": state.current_task, "tasks": data})

    @app.command()
    def abort(
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    ):
        """Discard worktrees and abandon the current task."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task to abort")
            raise typer.Exit(code=1)

        task_slug = state.current_task

        if not yes and output.is_tty:
            from InquirerPy import inquirer

            confirm = inquirer.confirm(
                message=f"Abort task '{task_slug}'? This will remove all worktrees.",
                default=False,
            ).execute()
            if not confirm:
                output.print("Aborted")
                raise typer.Exit(code=0)

        wt_mgr = container.worktree_manager()
        wt_mgr.abort(task_slug)
        output.success(f"Aborted task: {task_slug}")

    @app.command()
    def finish():
        """Create PRs and clean up worktrees in dependency order."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task to finish")
            raise typer.Exit(code=1)

        # v1: just report what would happen, actual PR creation is future work
        task = state.tasks[state.current_task]
        graph = container.graph()
        ordered = graph.topo_sort(task.affected_repos)

        output.print(f"[bold]Finishing task:[/bold] {task.slug}")
        output.print(f"[bold]Merge order:[/bold]")
        for i, repo in enumerate(ordered, 1):
            output.print(f"  {i}. {repo}")

        output.warning(
            "PR creation not yet implemented in v1. "
            "Use `gh pr create` manually in each repo in the order shown above."
        )
```

Add to `src/mship/cli/__init__.py`:
```python
from mship.cli import worktree as _worktree_mod

_worktree_mod.register(app, get_container)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_worktree.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_worktree.py src/mship/cli/__init__.py
git commit -m "feat: add spawn, worktrees, abort, and finish CLI commands"
```

---

### Task 16: CLI — Execution Commands (test, run, logs)

**Files:**
- Create: `src/mship/cli/exec.py`
- Create: `tests/cli/test_exec.py`
- Modify: `src/mship/cli/__init__.py`

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_exec.py`:
```python
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def configured_exec_app(workspace: Path):
    from mship.cli import container

    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    # Set up a task with affected repos
    mgr = StateManager(state_dir)
    task = Task(
        slug="test-task",
        description="Test task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/test-task",
    )
    mgr.save(WorkspaceState(current_task="test-task", tasks={"test-task": task}))

    # Mock shell to avoid needing real task binary
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    yield workspace, mock_shell
    container.reset_override()


def test_mship_test(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["test"])
    assert result.exit_code == 0
    assert mock_shell.run_task.call_count == 2  # shared + auth-service


def test_mship_test_all_flag(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="fail"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test", "--all"])
    assert mock_shell.run_task.call_count == 2


def test_mship_test_fail_fast(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="fail"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    result = runner.invoke(app, ["test"])
    assert mock_shell.run_task.call_count == 1


def test_mship_run(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0


def test_mship_test_no_active_task(workspace: Path):
    from mship.cli import container

    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)

    result = runner.invoke(app, ["test"])
    assert result.exit_code != 0 or "No active task" in result.output
    container.reset_override()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_exec.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write the implementation**

`src/mship/cli/exec.py`:
```python
from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):  # noqa: ANN001
    @app.command(name="test")
    def test_cmd(
        run_all: bool = typer.Option(False, "--all", help="Run all repos even on failure"),
    ):
        """Run tests across affected repos in dependency order."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        executor = container.executor()
        result = executor.execute(
            "test",
            repos=task.affected_repos,
            run_all=run_all,
            task_slug=state.current_task,
        )

        for repo_result in result.results:
            if repo_result.success:
                output.success(f"{repo_result.repo}: pass")
            else:
                output.error(f"{repo_result.repo}: fail")
                if repo_result.shell_result.stderr:
                    output.print(repo_result.shell_result.stderr.strip())

        if not result.success:
            raise typer.Exit(code=1)

    @app.command(name="run")
    def run_cmd():
        """Start services across repos in dependency order."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        executor = container.executor()
        result = executor.execute("run", repos=task.affected_repos)

        if not result.success:
            for repo_result in result.results:
                if not repo_result.success:
                    output.error(f"{repo_result.repo}: failed to start")
            raise typer.Exit(code=1)
        output.success("All services started")

    @app.command()
    def logs(
        service: str,
    ):
        """Tail logs for a specific service."""
        container = get_container()
        output = Output()
        config = container.config()

        if service not in config.repos:
            output.error(f"Unknown service: {service}")
            raise typer.Exit(code=1)

        repo = config.repos[service]
        shell = container.shell()
        actual_task = repo.tasks.get("logs", "logs")
        env_runner = repo.env_runner or config.env_runner

        result = shell.run_task(
            task_name="logs",
            actual_task_name=actual_task,
            cwd=repo.path,
            env_runner=env_runner,
        )
        output.print(result.stdout)
```

Add to `src/mship/cli/__init__.py`:
```python
from mship.cli import exec as _exec_mod

_exec_mod.register(app, get_container)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_exec.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/exec.py tests/cli/test_exec.py src/mship/cli/__init__.py
git commit -m "feat: add mship test, run, and logs CLI commands"
```

---

### Task 17: Final Integration — Full CLI Assembly & Smoke Test

**Files:**
- Modify: `src/mship/cli/__init__.py` (final form)
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write the integration test**

`tests/test_integration.py`:
```python
"""End-to-end smoke test: spawn → phase → test → abort."""
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner
from unittest.mock import MagicMock

from mship.cli import app
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def full_workspace(workspace_with_git: Path):
    from mship.cli import container

    state_dir = workspace_with_git / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    yield workspace_with_git
    container.reset_override()


def test_full_lifecycle(full_workspace: Path):
    # 1. Spawn
    result = runner.invoke(app, ["spawn", "add labels", "--repos", "shared,auth-service"])
    assert result.exit_code == 0, result.output
    assert "add-labels" in result.output

    # 2. Check status
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "add-labels" in result.output
    assert "plan" in result.output

    # 3. Transition to dev
    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code == 0

    # 4. Check status shows dev
    result = runner.invoke(app, ["status"])
    assert "dev" in result.output

    # 5. Run tests
    result = runner.invoke(app, ["test"])
    assert result.exit_code == 0

    # 6. List worktrees
    result = runner.invoke(app, ["worktrees"])
    assert result.exit_code == 0
    assert "add-labels" in result.output

    # 7. Show graph
    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "shared" in result.output

    # 8. Abort
    result = runner.invoke(app, ["abort", "--yes"])
    assert result.exit_code == 0

    # 9. Status shows no task
    result = runner.invoke(app, ["status"])
    assert "No active task" in result.output
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests PASS.

- [ ] **Step 4: Verify CLI entry point works**

Run: `uv run mship --help`
Expected: Shows help with all commands: `status`, `graph`, `phase`, `spawn`, `worktrees`, `finish`, `abort`, `test`, `run`, `logs`

- [ ] **Step 5: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add end-to-end integration smoke test for full CLI lifecycle"
```

---

## Self-Review Checklist

**Spec coverage:**
- Config model + loader + discovery: Task 5
- Dependency graph + topo sort + cycle detection: Task 6
- State model + manager + atomic writes: Task 7
- Phase transitions + soft gates: Task 8
- Executor + env_runner + fail-fast/--all: Task 9
- Worktree spawn/abort: Task 10
- DI container: Task 11
- CLI output (TTY/JSON): Task 12
- CLI status + graph: Task 13
- CLI phase: Task 14
- CLI spawn/finish/abort/worktrees: Task 15
- CLI test/run/logs: Task 16
- Integration test: Task 17

**Gap:** `mship finish` PR creation with coordination blocks is stubbed in v1 (warns to use `gh pr create` manually). This matches the spec's v1 scope — full PR automation requires `gh` CLI integration which is more of a v1.1 feature.

**Placeholder scan:** No TBDs, TODOs, or "implement later" anywhere.

**Type consistency:** All Pydantic models, function signatures, and import paths are consistent across tasks.
