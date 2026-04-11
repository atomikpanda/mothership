# Multi-Repo Scaling Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add repo filtering (`--repos`, `--tag`), parallel execution within dependency tiers, repo tags, and dependency types (compile/runtime) to make mothership practical for large multi-repo workspaces.

**Architecture:** Config model gets `Dependency` type and `tags` field. Graph gets `topo_tiers()` for parallel grouping. Executor switches from sequential `topo_sort` loop to parallel-per-tier using `ThreadPoolExecutor`. CLI commands get `--repos` and `--tag` filter flags.

**Tech Stack:** Python 3.14, Pydantic v2, `concurrent.futures.ThreadPoolExecutor` (stdlib)

---

## File Map

### Config model
- `src/mship/core/config.py` — modify: add `Dependency` model, update `depends_on` type with backward-compat validator, add `tags` field

### Graph
- `src/mship/core/graph.py` — modify: update to work with `Dependency` objects, add `topo_tiers()` method

### Executor
- `src/mship/core/executor.py` — modify: parallel tier execution, `_TYPE` env vars, batch test result saves

### CLI
- `src/mship/cli/exec.py` — modify: add `--repos` and `--tag` flags, resolve repos from filters

### Tests
- `tests/core/test_config.py` — modify: test Dependency model, tags, backward compat
- `tests/core/test_graph.py` — modify: test topo_tiers
- `tests/core/test_executor.py` — modify: test parallel execution, _TYPE env vars
- `tests/cli/test_exec.py` — modify: test --repos and --tag flags

---

### Task 1: Dependency Model & Tags in Config

**Files:**
- Modify: `src/mship/core/config.py`
- Modify: `tests/core/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_config.py`:
```python
from mship.core.config import WorkspaceConfig, RepoConfig, ConfigLoader, Dependency


def test_dependency_model():
    dep = Dependency(repo="shared", type="compile")
    assert dep.repo == "shared"
    assert dep.type == "compile"


def test_dependency_default_type():
    dep = Dependency(repo="shared")
    assert dep.type == "compile"


def test_depends_on_string_normalized(workspace: Path):
    """Plain string depends_on should be normalized to Dependency objects."""
    config = ConfigLoader.load(workspace / "mothership.yaml")
    deps = config.repos["auth-service"].depends_on
    assert len(deps) == 1
    assert isinstance(deps[0], Dependency)
    assert deps[0].repo == "shared"
    assert deps[0].type == "compile"


def test_depends_on_mixed_format(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
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
"""
    )
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
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
    tags: [apple, core]
"""
    )
    config = ConfigLoader.load(cfg)
    assert config.repos["shared"].tags == ["apple", "core"]


def test_repos_by_tag(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
    tags: [apple]
  auth-service:
    path: ./auth-service
    type: service
    tags: [apple, mobile]
  api-gateway:
    path: ./api-gateway
    type: service
    tags: [android]
"""
    )
    config = ConfigLoader.load(cfg)
    apple_repos = [name for name, repo in config.repos.items() if "apple" in repo.tags]
    assert set(apple_repos) == {"shared", "auth-service"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_config.py -v -k "dependency or tag or mixed"`
Expected: FAIL — `ImportError: cannot import name 'Dependency'`

- [ ] **Step 3: Write the implementation**

Replace the contents of `src/mship/core/config.py`:

```python
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator


class Dependency(BaseModel):
    repo: str
    type: Literal["compile", "runtime"] = "compile"


class RepoConfig(BaseModel):
    path: Path
    type: Literal["library", "service"]
    depends_on: list[Dependency] = []
    env_runner: str | None = None
    tasks: dict[str, str] = {}
    tags: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def normalize_depends_on(cls, data):
        """Normalize string depends_on entries to Dependency objects."""
        if isinstance(data, dict) and "depends_on" in data:
            normalized = []
            for dep in data["depends_on"]:
                if isinstance(dep, str):
                    normalized.append({"repo": dep, "type": "compile"})
                else:
                    normalized.append(dep)
            data["depends_on"] = normalized
        return data


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
                if dep.repo not in repo_names:
                    raise ValueError(
                        f"Repo '{name}' depends on '{dep.repo}' which does not exist. "
                        f"Valid repos: {sorted(repo_names)}"
                    )
        return self

    @model_validator(mode="after")
    def validate_no_cycles(self) -> "WorkspaceConfig":
        in_degree: dict[str, int] = {name: 0 for name in self.repos}
        adjacency: dict[str, list[str]] = {name: [] for name in self.repos}
        for name, repo in self.repos.items():
            for dep in repo.depends_on:
                adjacency[dep.repo].append(name)
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
Expected: All tests PASS (existing tests still work because string normalization is backward compatible).

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: Some tests may fail because `DependencyGraph` and `RepoExecutor` access `repo.depends_on` as strings — they now get `Dependency` objects. That's expected; we fix it in the next tasks.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/config.py tests/core/test_config.py
git commit -m "feat: add Dependency model with compile/runtime types, add tags to RepoConfig"
```

---

### Task 2: Update DependencyGraph for Dependency Objects + topo_tiers

**Files:**
- Modify: `src/mship/core/graph.py`
- Modify: `tests/core/test_graph.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_graph.py`:
```python
def test_topo_tiers_linear(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    tiers = graph.topo_tiers()
    assert len(tiers) == 3
    assert tiers[0] == ["shared"]
    assert tiers[1] == ["auth-service"]
    assert tiers[2] == ["api-gateway"]


def test_topo_tiers_parallel(workspace: Path):
    """Repos with same deps should be in the same tier."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
  api-gateway:
    path: ./api-gateway
    type: service
    depends_on: [shared]
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    tiers = graph.topo_tiers()
    assert len(tiers) == 2
    assert tiers[0] == ["shared"]
    assert set(tiers[1]) == {"auth-service", "api-gateway"}


def test_topo_tiers_subset(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    tiers = graph.topo_tiers(repos=["shared", "auth-service"])
    assert len(tiers) == 2
    assert tiers[0] == ["shared"]
    assert tiers[1] == ["auth-service"]


def test_topo_tiers_no_deps(workspace: Path):
    """All repos with no deps should be in tier 0."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
  auth-service:
    path: ./auth-service
    type: service
  api-gateway:
    path: ./api-gateway
    type: service
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    tiers = graph.topo_tiers()
    assert len(tiers) == 1
    assert set(tiers[0]) == {"shared", "auth-service", "api-gateway"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_graph.py -v -k "tiers"`
Expected: FAIL — `AttributeError: 'DependencyGraph' object has no attribute 'topo_tiers'`

- [ ] **Step 3: Write the implementation**

Replace `src/mship/core/graph.py`:

```python
from mship.core.config import WorkspaceConfig, Dependency


class DependencyGraph:
    """Repo dependency graph with topological sort and traversal."""

    def __init__(self, config: WorkspaceConfig) -> None:
        self._config = config
        self._forward: dict[str, list[str]] = {name: [] for name in config.repos}
        self._reverse: dict[str, list[str]] = {name: [] for name in config.repos}

        for name, repo in config.repos.items():
            for dep in repo.depends_on:
                dep_name = dep.repo if isinstance(dep, Dependency) else dep
                self._forward[dep_name].append(name)
                self._reverse[name].append(dep_name)

    def topo_sort(self, repos: list[str] | None = None) -> list[str]:
        """Return repos in dependency order (dependencies first)."""
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

    def topo_tiers(self, repos: list[str] | None = None) -> list[list[str]]:
        """Return repos grouped into dependency tiers.

        Each tier is a list of repos that can run concurrently.
        Tiers are ordered: tier N's deps are all in tiers 0..N-1.
        """
        target_set = set(repos) if repos else set(self._config.repos.keys())

        in_degree: dict[str, int] = {}
        for name in target_set:
            in_degree[name] = sum(
                1 for dep in self._reverse[name] if dep in target_set
            )

        tiers: list[list[str]] = []
        remaining = set(target_set)

        while remaining:
            # Current tier: all nodes with in_degree 0
            tier = sorted(n for n in remaining if in_degree[n] == 0)
            if not tier:
                break  # cycle (shouldn't happen — validated at config load)
            tiers.append(tier)
            for node in tier:
                remaining.discard(node)
                for neighbor in self._forward[node]:
                    if neighbor in remaining:
                        in_degree[neighbor] -= 1

        return tiers

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
Expected: All tests PASS (existing + new).

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: Should be closer to passing now. Executor tests may still need updates.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/graph.py tests/core/test_graph.py
git commit -m "feat: add topo_tiers for parallel execution, support Dependency objects in graph"
```

---

### Task 3: Update Executor — Parallel Tiers + _TYPE Env Vars

**Files:**
- Modify: `src/mship/core/executor.py`
- Modify: `tests/core/test_executor.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_executor.py`:
```python
from concurrent.futures import ThreadPoolExecutor


def test_upstream_env_includes_type(workspace: Path):
    """UPSTREAM_*_TYPE env var should be set based on dependency type."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
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
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    task = Task(
        slug="type-test",
        description="Test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "backend", "ios-app"],
        branch="feat/type-test",
        worktrees={
            "shared": Path("/tmp/shared-wt"),
            "backend": Path("/tmp/backend-wt"),
        },
    )
    state = WorkspaceState(current_task="type-test", tasks={"type-test": task})
    state_mgr.save(state)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    env = executor.resolve_upstream_env("ios-app", "type-test")
    assert env["UPSTREAM_SHARED"] == "/tmp/shared-wt"
    assert env["UPSTREAM_SHARED_TYPE"] == "compile"
    assert env["UPSTREAM_BACKEND"] == "/tmp/backend-wt"
    assert env["UPSTREAM_BACKEND_TYPE"] == "runtime"


def test_execute_parallel_tiers(workspace: Path):
    """Repos in same tier should run (we verify all ran, order within tier is non-deterministic)."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
  api-gateway:
    path: ./api-gateway
    type: service
    depends_on: [shared]
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
    result = executor.execute("test", repos=["shared", "auth-service", "api-gateway"])
    assert result.success
    assert mock_shell.run_task.call_count == 3
    repos_run = {c.kwargs["cwd"].name for c in mock_shell.run_task.call_args_list}
    assert repos_run == {"shared", "auth-service", "api-gateway"}


def test_execute_parallel_failfast_between_tiers(workspace: Path):
    """If tier 0 fails, tier 1 should not run."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=1, stdout="", stderr="fail")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute("test", repos=["shared", "auth-service"])
    assert not result.success
    # Only tier 0 (shared) should have run
    assert mock_shell.run_task.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_executor.py -v -k "type or parallel"`
Expected: FAIL

- [ ] **Step 3: Write the implementation**

Replace `src/mship/core/executor.py`:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mship.core.config import WorkspaceConfig, Dependency
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
    """Execute tasks across repos in dependency order, parallel within tiers."""

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

    def resolve_upstream_env(
        self, repo_name: str, task_slug: str | None
    ) -> dict[str, str]:
        """Compute UPSTREAM_* and UPSTREAM_*_TYPE env vars."""
        if task_slug is None:
            return {}
        state = self._state_manager.load()
        task = state.tasks.get(task_slug)
        if task is None or not task.worktrees:
            return {}

        env: dict[str, str] = {}
        repo_config = self._config.repos[repo_name]
        for dep in repo_config.depends_on:
            dep_name = dep.repo if isinstance(dep, Dependency) else dep
            dep_type = dep.type if isinstance(dep, Dependency) else "compile"
            if dep_name in task.worktrees:
                var_name = f"UPSTREAM_{dep_name.upper().replace('-', '_')}"
                env[var_name] = str(task.worktrees[dep_name])
                env[f"{var_name}_TYPE"] = dep_type
        return env

    def _resolve_cwd(self, repo_name: str, task_slug: str | None) -> Path:
        """Get execution directory: worktree if available, otherwise repo path."""
        repo_config = self._config.repos[repo_name]
        cwd = repo_config.path
        if task_slug:
            state = self._state_manager.load()
            task = state.tasks.get(task_slug)
            if task and repo_name in task.worktrees:
                wt_path = Path(task.worktrees[repo_name])
                if wt_path.exists():
                    cwd = wt_path
        return cwd

    def _execute_one(
        self,
        repo_name: str,
        canonical_task: str,
        task_slug: str | None,
    ) -> RepoResult:
        """Execute a single repo's task. Thread-safe."""
        actual_name = self.resolve_task_name(repo_name, canonical_task)
        env_runner = self.resolve_env_runner(repo_name)
        upstream_env = self.resolve_upstream_env(repo_name, task_slug)
        cwd = self._resolve_cwd(repo_name, task_slug)

        shell_result = self._shell.run_task(
            task_name=canonical_task,
            actual_task_name=actual_name,
            cwd=cwd,
            env_runner=env_runner,
            env=upstream_env or None,
        )

        return RepoResult(
            repo=repo_name,
            task_name=actual_name,
            shell_result=shell_result,
        )

    def execute(
        self,
        canonical_task: str,
        repos: list[str],
        run_all: bool = False,
        task_slug: str | None = None,
    ) -> ExecutionResult:
        tiers = self._graph.topo_tiers(repos)
        result = ExecutionResult()

        for tier in tiers:
            tier_results: list[RepoResult] = []

            if len(tier) == 1:
                # Single repo in tier — no threading overhead
                repo_result = self._execute_one(tier[0], canonical_task, task_slug)
                tier_results.append(repo_result)
            else:
                # Multiple repos — run in parallel
                with ThreadPoolExecutor(max_workers=len(tier)) as pool:
                    futures = {
                        pool.submit(self._execute_one, repo_name, canonical_task, task_slug): repo_name
                        for repo_name in tier
                    }
                    for future in as_completed(futures):
                        tier_results.append(future.result())

            # Sort tier results for deterministic output order
            tier_results.sort(key=lambda r: r.repo)
            result.results.extend(tier_results)

            # Batch-save test results for this tier
            if task_slug and canonical_task == "test":
                state = self._state_manager.load()
                task = state.tasks.get(task_slug)
                if task:
                    for repo_result in tier_results:
                        task.test_results[repo_result.repo] = TestResult(
                            status="pass" if repo_result.success else "fail",
                            at=datetime.now(timezone.utc),
                        )
                    self._state_manager.save(state)

            # Fail-fast between tiers
            tier_success = all(r.success for r in tier_results)
            if not tier_success and not run_all:
                break

        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_executor.py -v`
Expected: All tests PASS (existing + new). Existing tests work because single-repo tiers behave identically to sequential execution.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/executor.py tests/core/test_executor.py
git commit -m "feat: parallel execution within dependency tiers, UPSTREAM_*_TYPE env vars"
```

---

### Task 4: CLI — `--repos` and `--tag` Flags

**Files:**
- Modify: `src/mship/cli/exec.py`
- Modify: `tests/cli/test_exec.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/cli/test_exec.py`:
```python
def test_mship_test_repos_filter(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["test", "--repos", "shared"])
    assert result.exit_code == 0
    # Should only run shared, not auth-service
    assert mock_shell.run_task.call_count == 1


def test_mship_test_tag_filter(workspace: Path):
    """Test --tag flag filters repos by tag."""
    from mship.cli import container

    # Create config with tags
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
    tags: [apple]
  auth-service:
    path: ./auth-service
    type: service
    tags: [apple, mobile]
  api-gateway:
    path: ./api-gateway
    type: service
    tags: [android]
"""
    )

    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    mgr = StateManager(state_dir)
    task = Task(
        slug="tag-test",
        description="Tag test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service", "api-gateway"],
        branch="feat/tag-test",
    )
    mgr.save(WorkspaceState(current_task="tag-test", tasks={"tag-test": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    container.shell.override(mock_shell)

    result = runner.invoke(app, ["test", "--tag", "apple"])
    assert result.exit_code == 0
    # Should run shared + auth-service (both tagged apple), not api-gateway
    assert mock_shell.run_task.call_count == 2

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_mship_test_unknown_repo_errors(configured_exec_app):
    workspace, mock_shell = configured_exec_app
    result = runner.invoke(app, ["test", "--repos", "nonexistent"])
    assert result.exit_code != 0 or "unknown" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_exec.py -v -k "filter or tag or unknown"`
Expected: FAIL — no `--repos` flag on test command

- [ ] **Step 3: Write the implementation**

Replace `src/mship/cli/exec.py`:

```python
from typing import Optional

import typer

from mship.cli.output import Output


def _resolve_repos(
    config, task_affected: list[str],
    repos_filter: str | None, tag_filter: list[str] | None,
) -> list[str]:
    """Resolve target repos from --repos and --tag filters."""
    candidates = None

    if repos_filter:
        candidates = set(repos_filter.split(","))
        # Validate repo names exist in config
        for name in candidates:
            if name not in config.repos:
                raise ValueError(f"Unknown repo: {name}")

    if tag_filter:
        tagged = set()
        for name, repo in config.repos.items():
            if any(t in repo.tags for t in tag_filter):
                tagged.add(name)
        if candidates is not None:
            candidates = candidates & tagged  # AND: must match both
        else:
            candidates = tagged

    if candidates is not None:
        return list(candidates)
    return task_affected


def register(app: typer.Typer, get_container):
    @app.command(name="test")
    def test_cmd(
        run_all: bool = typer.Option(False, "--all", help="Run all repos even on failure"),
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names to filter"),
        tag: Optional[list[str]] = typer.Option(None, "--tag", help="Filter repos by tag"),
    ):
        """Run tests across affected repos in dependency order."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn \"description\"` to start one.")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        config = container.config()

        try:
            target_repos = _resolve_repos(config, task.affected_repos, repos, tag)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        executor = container.executor()
        result = executor.execute(
            "test",
            repos=target_repos,
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
    def run_cmd(
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names to filter"),
        tag: Optional[list[str]] = typer.Option(None, "--tag", help="Filter repos by tag"),
    ):
        """Start services across repos in dependency order."""
        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        if state.current_task is None:
            output.error("No active task. Run `mship spawn \"description\"` to start one.")
            raise typer.Exit(code=1)

        task = state.tasks[state.current_task]
        config = container.config()

        try:
            target_repos = _resolve_repos(config, task.affected_repos, repos, tag)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        executor = container.executor()
        result = executor.execute("run", repos=target_repos)

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

        from pathlib import Path
        cwd = repo.path
        state_mgr = container.state_manager()
        state = state_mgr.load()
        if state.current_task:
            task = state.tasks.get(state.current_task)
            if task and service in task.worktrees:
                wt_path = Path(task.worktrees[service])
                if wt_path.exists():
                    cwd = wt_path

        result = shell.run_task(
            task_name="logs",
            actual_task_name=actual_task,
            cwd=cwd,
            env_runner=env_runner,
        )
        output.print(result.stdout)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_exec.py -v`
Expected: All tests PASS (existing + new).

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/exec.py tests/cli/test_exec.py
git commit -m "feat: add --repos and --tag filters to test and run commands"
```

---

### Task 5: Fix Remaining Dependency Object References

**Files:**
- Modify: `src/mship/core/worktree.py` (if it accesses depends_on as strings)
- Modify: `src/mship/core/handoff.py` (repo_deps uses depends_on)
- Modify: `src/mship/cli/worktree.py` (finish builds repo_deps)
- Modify: any other files that access `repo.depends_on` as strings

- [ ] **Step 1: Search for all depends_on usages**

Run: `grep -rn "depends_on" src/mship/ --include="*.py"`

Check each usage — any that iterates `depends_on` and treats elements as strings needs updating to handle `Dependency` objects.

- [ ] **Step 2: Fix `cli/worktree.py` finish command**

The finish command builds `repo_deps` for handoff:
```python
repo_deps = {name: config.repos[name].depends_on for name in ordered}
```

This now returns `list[Dependency]` objects instead of `list[str]`. Fix:
```python
repo_deps = {
    name: [d.repo for d in config.repos[name].depends_on]
    for name in ordered
}
```

- [ ] **Step 3: Fix any other references**

Check `core/handoff.py` — `generate_handoff` receives `repo_deps: dict[str, list[str]]` so it expects strings. The fix in step 2 handles this.

Check `cli/status.py`, `cli/init.py` — these don't access `depends_on` directly.

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/
git commit -m "fix: update all depends_on references to handle Dependency objects"
```

---

### Task 6: Integration Test

**Files:**
- Create: `tests/test_scaling_integration.py`

- [ ] **Step 1: Write the integration test**

`tests/test_scaling_integration.py`:
```python
"""Integration test: parallel tiers, tag filtering, dependency types."""
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime, timezone

import pytest
import yaml
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def metarepo_workspace(tmp_path: Path):
    """Create a 5-repo metarepo-style workspace with tags and dep types."""
    for name in ["shared-swift", "backend", "ios-app", "android-app", "macos-app"]:
        d = tmp_path / name
        d.mkdir()
        (d / "Taskfile.yml").write_text(f"version: '3'\ntasks:\n  test:\n    cmds:\n      - echo {name}\n")
        subprocess.run(["git", "init", str(d)], check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=d, check=True, capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com"},
        )

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: my-platform
repos:
  shared-swift:
    path: ./shared-swift
    type: library
    tags: [apple]
  backend:
    path: ./backend
    type: service
    tags: [backend]
  ios-app:
    path: ./ios-app
    type: service
    tags: [apple, mobile]
    depends_on:
      - repo: shared-swift
        type: compile
      - repo: backend
        type: runtime
  android-app:
    path: ./android-app
    type: service
    tags: [android, mobile]
    depends_on:
      - repo: backend
        type: runtime
  macos-app:
    path: ./macos-app
    type: service
    tags: [apple, desktop]
    depends_on:
      - repo: shared-swift
        type: compile
      - repo: backend
        type: runtime
"""
    )

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    container.shell.override(mock_shell)

    yield tmp_path, mock_shell

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_metarepo_spawn_and_test_all(metarepo_workspace):
    workspace, mock_shell = metarepo_workspace

    result = runner.invoke(app, ["spawn", "add user feed"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["test"])
    assert result.exit_code == 0, result.output
    # All 5 repos should have been tested
    assert mock_shell.run_task.call_count >= 5


def test_metarepo_test_tag_apple(metarepo_workspace):
    workspace, mock_shell = metarepo_workspace

    runner.invoke(app, ["spawn", "apple only test"])
    mock_shell.run_task.reset_mock()

    result = runner.invoke(app, ["test", "--tag", "apple"])
    assert result.exit_code == 0, result.output
    # shared-swift, ios-app, macos-app = 3 repos
    repos_tested = {c.kwargs["cwd"].name for c in mock_shell.run_task.call_args_list}
    assert "shared-swift" in repos_tested
    assert "ios-app" in repos_tested
    assert "macos-app" in repos_tested
    assert "android-app" not in repos_tested
    assert "backend" not in repos_tested


def test_metarepo_test_repos_filter(metarepo_workspace):
    workspace, mock_shell = metarepo_workspace

    runner.invoke(app, ["spawn", "repos filter test"])
    mock_shell.run_task.reset_mock()

    result = runner.invoke(app, ["test", "--repos", "backend"])
    assert result.exit_code == 0, result.output
    assert mock_shell.run_task.call_count == 1


def test_metarepo_graph(metarepo_workspace):
    workspace, mock_shell = metarepo_workspace

    result = runner.invoke(app, ["graph"])
    assert result.exit_code == 0
    assert "shared-swift" in result.output
    assert "backend" in result.output
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_scaling_integration.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 3: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_scaling_integration.py
git commit -m "test: add integration tests for multi-repo scaling features"
```

---

## Self-Review

**Spec coverage:**
- `--repos` filter on test/run: Task 4
- `--tag` filter on test/run: Task 4
- `--repos` and `--tag` combine as AND: Task 4 (`_resolve_repos`)
- `--tag` OR semantics for multiple tags: Task 4 (`any(t in repo.tags for t in tag_filter)`)
- No validation against task affected repos: Task 4 (validates against config, not task)
- Unknown repo errors: Task 4 (test + implementation)
- Parallel within tiers: Task 3 (`ThreadPoolExecutor`)
- Fail-fast between tiers: Task 3 (check tier success before next tier)
- `--all` continues across tiers: Task 3 (preserved)
- Batch test result saves per tier: Task 3 (one save per tier)
- `topo_tiers()` method: Task 2
- `tags` on RepoConfig: Task 1
- `Dependency` model with type: Task 1
- Backward compat (string depends_on): Task 1 (normalizer)
- `UPSTREAM_*_TYPE` env vars: Task 3
- Both dep types affect execution order: Task 2 (graph treats all deps same)
- Fix all depends_on string references: Task 5

**Placeholder scan:** No TBDs. All code is complete.

**Type consistency:** `Dependency.repo` (str), `Dependency.type` (Literal["compile", "runtime"]). `topo_tiers` returns `list[list[str]]`. `_resolve_repos` returns `list[str]`. `resolve_upstream_env` handles both `Dependency` and str via isinstance check. All consistent.
