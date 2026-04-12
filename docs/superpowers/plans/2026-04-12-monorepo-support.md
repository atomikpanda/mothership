# Monorepo Support & Service Runtime Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add monorepo subdirectory service support via `git_root`, background service launching via `start_mode`, and fix `mship doctor` to resolve task name aliases.

**Architecture:** `git_root` makes `path` relative-at-runtime; the executor's `_resolve_cwd` computes effective paths by chaining through `git_root`. `start_mode: background` uses `Popen` + a background tracker that `mship run` awaits with signal forwarding. Doctor reads `repo.tasks` mapping before checking Taskfile output.

**Tech Stack:** Python 3.14, Pydantic v2 (existing), `subprocess.Popen` (stdlib), `signal` (stdlib)

---

## File Map

### Core
- `src/mship/core/config.py` — add `git_root` and `start_mode` fields; make `path` validation conditional on `git_root`
- `src/mship/core/worktree.py` — spawn skips `git_root` repos for worktree creation; computes their effective worktree path
- `src/mship/core/executor.py` — `_resolve_cwd` chains through `git_root`; `_execute_one` branches on `start_mode`; track background processes
- `src/mship/core/doctor.py` — resolve `tasks:` mapping when checking standard tasks

### Util
- `src/mship/util/shell.py` — `run_streaming` already exists; no changes needed unless we need a helper for signal forwarding

### CLI
- `src/mship/cli/exec.py` — `mship run` awaits background subprocesses with SIGINT forwarding

### Docs
- `README.md` — add "Monorepo Support", "Service Start Modes", "Task Name Aliasing" subsections

### Tests
- `tests/core/test_config.py` — test `git_root`, `start_mode` validation and defaults
- `tests/core/test_worktree.py` — test spawn skips git_root repos, computes subdir paths
- `tests/core/test_executor.py` — test cwd resolution through git_root, background launch
- `tests/core/test_doctor.py` — test task alias resolution
- `tests/test_monorepo_integration.py` — end-to-end monorepo workspace test

---

### Task 1: Add `git_root` and `start_mode` Fields to RepoConfig

**Files:**
- Modify: `src/mship/core/config.py`
- Modify: `tests/core/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_config.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_config.py -v -k "git_root or start_mode"`
Expected: FAIL — `git_root` is not a valid field, `start_mode` is not a valid field.

- [ ] **Step 3: Add fields and validation to `src/mship/core/config.py`**

Replace the `RepoConfig` class with:

```python
class RepoConfig(BaseModel):
    path: Path
    type: Literal["library", "service"]
    depends_on: list[Dependency] = []
    env_runner: str | None = None
    tasks: dict[str, str] = {}
    tags: list[str] = []
    git_root: str | None = None
    start_mode: Literal["foreground", "background"] = "foreground"

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
```

- [ ] **Step 4: Add `git_root` validation to WorkspaceConfig**

In `src/mship/core/config.py`, add this validator to `WorkspaceConfig` (after `validate_no_cycles`):

```python
    @model_validator(mode="after")
    def validate_git_root_refs(self) -> "WorkspaceConfig":
        repo_names = set(self.repos.keys())
        for name, repo in self.repos.items():
            if repo.git_root is None:
                continue
            if repo.git_root not in repo_names:
                raise ValueError(
                    f"Repo '{name}' has git_root '{repo.git_root}' which does not exist. "
                    f"Valid repos: {sorted(repo_names)}"
                )
            # No chaining: the referenced repo cannot itself have git_root set
            parent = self.repos[repo.git_root]
            if parent.git_root is not None:
                raise ValueError(
                    f"Repo '{name}' git_root '{repo.git_root}' is itself a subdirectory service. "
                    f"Cannot chain git_root references."
                )
        return self
```

- [ ] **Step 5: Update `ConfigLoader.load` to skip path resolution for `git_root` repos**

Replace the `load` method:

```python
    @staticmethod
    def load(path: Path) -> WorkspaceConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)

        workspace_root = path.parent

        config = WorkspaceConfig(**raw)

        # First pass: resolve paths and validate for repos WITHOUT git_root
        for name, repo in config.repos.items():
            if repo.git_root is not None:
                continue
            resolved = (workspace_root / repo.path).resolve()
            repo.path = resolved
            if not resolved.is_dir():
                raise ValueError(f"Repo '{name}' path does not exist: {resolved}")
            if not (resolved / "Taskfile.yml").exists():
                raise ValueError(
                    f"Repo '{name}' at {resolved} has no Taskfile.yml"
                )

        # Second pass: validate git_root repos against their parent's resolved path
        for name, repo in config.repos.items():
            if repo.git_root is None:
                continue
            parent = config.repos[repo.git_root]
            effective = (parent.path / repo.path).resolve()
            if not effective.is_dir():
                raise ValueError(
                    f"Repo '{name}' subdirectory does not exist: {effective}"
                )
            if not (effective / "Taskfile.yml").exists():
                raise ValueError(
                    f"Repo '{name}' at {effective} has no Taskfile.yml"
                )

        return config
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_config.py -v`
Expected: All tests PASS.

- [ ] **Step 7: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS (other modules don't access `git_root` or `start_mode` yet).

- [ ] **Step 8: Commit**

```bash
git add src/mship/core/config.py tests/core/test_config.py
git commit -m "feat: add git_root and start_mode fields to RepoConfig"
```

---

### Task 2: Skip `git_root` Repos in Worktree Spawn, Compute Effective Paths

**Files:**
- Modify: `src/mship/core/worktree.py`
- Modify: `tests/core/test_worktree.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_worktree.py`:

```python
def test_spawn_skips_git_root_repos(tmp_path: Path):
    """git_root repos don't get their own worktree — they share the parent's."""
    import os
    import subprocess

    # Create a real git repo with a subdirectory
    root = tmp_path / "monorepo"
    root.mkdir()
    (root / "Taskfile.yml").write_text("version: '3'")
    web = root / "web"
    web.mkdir()
    (web / "Taskfile.yml").write_text("version: '3'")
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=root, check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )

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
    from mship.core.config import ConfigLoader
    from mship.core.graph import DependencyGraph
    from mship.core.state import StateManager
    from mship.core.log import LogManager
    from mship.util.git import GitRunner
    from mship.util.shell import ShellRunner, ShellResult
    from unittest.mock import MagicMock

    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    log = MagicMock(spec=LogManager)

    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("mono test", repos=["root", "web"])

    state = state_mgr.load()
    task = state.tasks["mono-test"]

    # root gets a worktree at <root>/.worktrees/feat/mono-test
    assert "root" in task.worktrees
    root_wt = Path(task.worktrees["root"])
    assert root_wt.exists()
    # web's worktree is a subdirectory of root's worktree
    assert "web" in task.worktrees
    web_wt = Path(task.worktrees["web"])
    assert web_wt == root_wt / "web"
    assert web_wt.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_worktree.py -v -k "git_root"`
Expected: FAIL — `spawn` tries to create a worktree for the `git_root` repo and fails.

- [ ] **Step 3: Update `WorktreeManager.spawn` to skip `git_root` repos**

Replace the spawn method loop body in `src/mship/core/worktree.py`:

```python
    def spawn(
        self,
        description: str,
        repos: list[str] | None = None,
    ) -> Task:
        slug = slugify(description)
        branch = self._config.branch_pattern.replace("{slug}", slug)

        state = self._state_manager.load()
        if slug in state.tasks:
            raise ValueError(
                f"Task '{slug}' already exists. "
                f"Run `mship abort --yes` to remove it first, or use a different description."
            )

        if repos is None:
            repos = list(self._config.repos.keys())

        ordered = self._graph.topo_sort(repos)

        worktrees: dict[str, Path] = {}
        for repo_name in ordered:
            repo_config = self._config.repos[repo_name]

            if repo_config.git_root is not None:
                # Subdirectory service: share parent's worktree
                parent_wt = worktrees.get(repo_config.git_root)
                if parent_wt is None:
                    # Parent wasn't spawned in this call — use its config path
                    parent_wt = self._config.repos[repo_config.git_root].path
                effective = parent_wt / repo_config.path
                worktrees[repo_name] = effective

                # Run setup task in the subdirectory
                actual_setup = repo_config.tasks.get("setup", "setup")
                self._shell.run_task(
                    task_name="setup",
                    actual_task_name=actual_setup,
                    cwd=effective,
                    env_runner=repo_config.env_runner or self._config.env_runner,
                )
                continue

            # Normal repo: create its own worktree
            repo_path = repo_config.path

            if not self._git.is_ignored(repo_path, ".worktrees"):
                self._git.add_to_gitignore(repo_path, ".worktrees")

            wt_path = repo_path / ".worktrees" / branch
            self._git.worktree_add(
                repo_path=repo_path,
                worktree_path=wt_path,
                branch=branch,
            )
            worktrees[repo_name] = wt_path

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

        self._log.create(slug)
        self._log.append(
            slug,
            f"Task spawned. Repos: {', '.join(ordered)}. Branch: {branch}",
        )

        return task
```

- [ ] **Step 4: Update `abort` to skip `git_root` repos in worktree removal**

Replace the abort method loop in `src/mship/core/worktree.py`:

```python
    def abort(self, task_slug: str) -> None:
        state = self._state_manager.load()
        task = state.tasks[task_slug]

        for repo_name, wt_path in task.worktrees.items():
            repo_config = self._config.repos[repo_name]

            # Skip git_root repos — their "worktree" is just a subdirectory
            # of the parent's worktree and will disappear with it
            if repo_config.git_root is not None:
                continue

            try:
                self._git.worktree_remove(
                    repo_path=repo_config.path,
                    worktree_path=Path(wt_path),
                )
            except Exception:
                import shutil
                shutil.rmtree(Path(wt_path), ignore_errors=True)
            try:
                self._git.branch_delete(
                    repo_path=repo_config.path,
                    branch=task.branch,
                )
            except Exception:
                pass

        del state.tasks[task_slug]
        if state.current_task == task_slug:
            state.current_task = None
        self._state_manager.save(state)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_worktree.py -v`
Expected: All tests PASS (existing + new).

- [ ] **Step 6: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat: skip git_root repos in worktree spawn, compute subdir paths"
```

---

### Task 3: Update Executor to Resolve `git_root` Paths

**Files:**
- Modify: `src/mship/core/executor.py`
- Modify: `tests/core/test_executor.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_executor.py`:

```python
def test_cwd_resolves_through_git_root(tmp_path: Path):
    """When git_root is set and no worktree, cwd is parent.path / child.path."""
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
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    cwd = executor._resolve_cwd("web", None)
    assert cwd == web
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_executor.py -v -k "git_root"`
Expected: FAIL — `_resolve_cwd` returns `repo.path` which is `"web"` (relative, not absolute).

- [ ] **Step 3: Update `_resolve_cwd` to handle `git_root`**

Replace `_resolve_cwd` in `src/mship/core/executor.py`:

```python
    def _resolve_cwd(self, repo_name: str, task_slug: str | None) -> Path:
        """Get execution directory: worktree if available, otherwise resolved path.

        For repos with git_root set, path is resolved as parent_path / path.
        """
        repo_config = self._config.repos[repo_name]

        # If worktree exists in state, prefer it
        if task_slug:
            state = self._state_manager.load()
            task = state.tasks.get(task_slug)
            if task and repo_name in task.worktrees:
                wt_path = Path(task.worktrees[repo_name])
                if wt_path.exists():
                    return wt_path

        # No worktree: compute effective path
        if repo_config.git_root is not None:
            parent = self._config.repos[repo_config.git_root]
            return parent.path / repo_config.path
        return repo_config.path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_executor.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/executor.py tests/core/test_executor.py
git commit -m "feat: executor resolves cwd through git_root for subdirectory services"
```

---

### Task 4: Background Service Launch in Executor

**Files:**
- Modify: `src/mship/core/executor.py`
- Modify: `tests/core/test_executor.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_executor.py`:

```python
def test_background_start_mode_uses_popen(workspace: Path):
    """start_mode: background should call run_streaming, not run_task."""
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
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    # Mock streaming: returns a Popen-like object
    popen_mock = MagicMock()
    popen_mock.pid = 12345
    mock_shell.run_streaming.return_value = popen_mock

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute("run", repos=["shared"])

    assert result.success
    # run_task should NOT have been called
    mock_shell.run_task.assert_not_called()
    # run_streaming SHOULD have been called
    mock_shell.run_streaming.assert_called_once()


def test_foreground_start_mode_uses_run_task(workspace: Path):
    """Default start_mode is foreground — uses run_task."""
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute("run", repos=["shared"])
    mock_shell.run_task.assert_called_once()
    mock_shell.run_streaming.assert_not_called()


def test_background_returns_in_execution_result(workspace: Path):
    """ExecutionResult should include the background Popen handles."""
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
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 12345
    mock_shell.run_streaming.return_value = popen_mock

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute("run", repos=["shared"])

    assert len(result.background_processes) == 1
    assert result.background_processes[0] is popen_mock
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_executor.py -v -k "background or foreground_start"`
Expected: FAIL — `start_mode` isn't handled in executor yet, `background_processes` doesn't exist.

- [ ] **Step 3: Update `ExecutionResult` to track background processes**

In `src/mship/core/executor.py`, update the dataclass:

```python
@dataclass
class ExecutionResult:
    results: list[RepoResult] = field(default_factory=list)
    background_processes: list = field(default_factory=list)  # list[Popen]

    @property
    def success(self) -> bool:
        return all(r.success for r in self.results)
```

- [ ] **Step 4: Update `_execute_one` to branch on `start_mode`**

Replace `_execute_one` in `src/mship/core/executor.py`:

```python
    def _execute_one(
        self,
        repo_name: str,
        canonical_task: str,
        task_slug: str | None,
    ) -> tuple[RepoResult, object | None]:
        """Execute a single repo's task. Thread-safe.

        Returns (RepoResult, background_process_or_None).
        """
        actual_name = self.resolve_task_name(repo_name, canonical_task)
        env_runner = self.resolve_env_runner(repo_name)
        upstream_env = self.resolve_upstream_env(repo_name, task_slug)
        cwd = self._resolve_cwd(repo_name, task_slug)
        repo_config = self._config.repos[repo_name]

        if repo_config.start_mode == "background" and canonical_task == "run":
            # Launch as background subprocess, don't wait
            command = self._shell.build_command(
                f"task {actual_name}", env_runner
            )
            popen = self._shell.run_streaming(command, cwd=cwd)
            # Mark as "success" — we launched it. If it crashes later, that's
            # surfaced via Ctrl-C or process exit observation.
            return (
                RepoResult(
                    repo=repo_name,
                    task_name=actual_name,
                    shell_result=ShellResult(returncode=0, stdout="", stderr=""),
                ),
                popen,
            )

        shell_result = self._shell.run_task(
            task_name=canonical_task,
            actual_task_name=actual_name,
            cwd=cwd,
            env_runner=env_runner,
            env=upstream_env or None,
        )

        return (
            RepoResult(
                repo=repo_name,
                task_name=actual_name,
                shell_result=shell_result,
            ),
            None,
        )
```

- [ ] **Step 5: Update `execute` to collect background processes**

Replace `execute` in `src/mship/core/executor.py`:

```python
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
            tier_backgrounds: list = []

            if len(tier) == 1:
                repo_result, bg = self._execute_one(tier[0], canonical_task, task_slug)
                tier_results.append(repo_result)
                if bg is not None:
                    tier_backgrounds.append(bg)
            else:
                with ThreadPoolExecutor(max_workers=len(tier)) as pool:
                    futures = {
                        pool.submit(self._execute_one, repo_name, canonical_task, task_slug): repo_name
                        for repo_name in tier
                    }
                    for future in as_completed(futures):
                        repo_result, bg = future.result()
                        tier_results.append(repo_result)
                        if bg is not None:
                            tier_backgrounds.append(bg)

            tier_results.sort(key=lambda r: r.repo)
            result.results.extend(tier_results)
            result.background_processes.extend(tier_backgrounds)

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

            tier_success = all(r.success for r in tier_results)
            if not tier_success and not run_all:
                break

        return result
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_executor.py -v`
Expected: All tests PASS (existing + new).

- [ ] **Step 7: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
git add src/mship/core/executor.py tests/core/test_executor.py
git commit -m "feat: launch background services via run_streaming, track Popen handles"
```

---

### Task 5: `mship run` Waits for Background Services with Signal Forwarding

**Files:**
- Modify: `src/mship/cli/exec.py`
- Modify: `tests/cli/test_exec.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/cli/test_exec.py`:

```python
def test_mship_run_waits_for_background_services(workspace: Path):
    """mship run should block on background services, not exit immediately."""
    from mship.cli import container
    from datetime import datetime, timezone

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
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="bg-test",
        description="Background test",
        phase="dev",
        created_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/bg-test",
    )
    mgr.save(WorkspaceState(current_task="bg-test", tasks={"bg-test": task}))

    # Mock shell: run_streaming returns a Popen-like object that exits immediately
    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 12345
    popen_mock.wait.return_value = 0  # exits cleanly
    popen_mock.poll.return_value = 0
    mock_shell.run_streaming.return_value = popen_mock
    mock_shell.build_command.return_value = "task run"
    container.shell.override(mock_shell)

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0

    # Should have called wait() on the background process
    popen_mock.wait.assert_called()

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_exec.py -v -k "background"`
Expected: FAIL — mship run doesn't wait for background processes.

- [ ] **Step 3: Update `run_cmd` in `src/mship/cli/exec.py`**

Replace the `run_cmd` function:

```python
    @app.command(name="run")
    def run_cmd(
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names to filter"),
        tag: Optional[list[str]] = typer.Option(None, "--tag", help="Filter repos by tag"),
    ):
        """Start services across repos in dependency order."""
        import signal

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
            # Terminate any background processes that did start
            for proc in result.background_processes:
                try:
                    proc.terminate()
                except Exception:
                    pass
            raise typer.Exit(code=1)

        if not result.background_processes:
            output.success("All services started")
            return

        # Have background services — wait for them with signal forwarding
        output.success(f"Started {len(result.background_processes)} background service(s). Press Ctrl-C to stop.")

        def _forward_sigint(signum, frame):
            for proc in result.background_processes:
                try:
                    proc.send_signal(signal.SIGINT)
                except Exception:
                    pass

        signal.signal(signal.SIGINT, _forward_sigint)

        try:
            for proc in result.background_processes:
                proc.wait()
        except KeyboardInterrupt:
            for proc in result.background_processes:
                try:
                    proc.send_signal(signal.SIGINT)
                except Exception:
                    pass
            for proc in result.background_processes:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        output.print("All background services have exited")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_exec.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/exec.py tests/cli/test_exec.py
git commit -m "feat: mship run waits on background services, forwards SIGINT on Ctrl-C"
```

---

### Task 6: Doctor Resolves Task Name Aliases

**Files:**
- Modify: `src/mship/core/doctor.py`
- Modify: `tests/core/test_doctor.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/core/test_doctor.py`:

```python
def test_doctor_resolves_task_aliases(tmp_path: Path):
    """Doctor should check the aliased task name, not the canonical name."""
    repo_dir = tmp_path / "my-app"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  my-app:
    path: ./my-app
    type: service
    tasks:
      run: dev
"""
    )
    config = ConfigLoader.load(cfg)

    mock_shell = MagicMock(spec=ShellRunner)
    # task --list output contains "dev" but not "run"
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        ShellResult(returncode=0, stdout="test\ndev\nlint\nsetup\n", stderr="")
        if "task --list" in cmd
        else ShellResult(returncode=0, stdout="Logged in", stderr="")
    )

    checker = DoctorChecker(config, mock_shell)
    report = checker.run()

    # The "run" check should pass because "dev" exists (it's the alias)
    run_check = next(c for c in report.checks if c.name == "my-app/task:run")
    assert run_check.status == "pass"
    assert "dev" in run_check.message


def test_doctor_warns_when_alias_missing(tmp_path: Path):
    repo_dir = tmp_path / "my-app"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  my-app:
    path: ./my-app
    type: service
    tasks:
      run: nonexistent
"""
    )
    config = ConfigLoader.load(cfg)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        ShellResult(returncode=0, stdout="test\nlint\nsetup\n", stderr="")
        if "task --list" in cmd
        else ShellResult(returncode=0, stdout="Logged in", stderr="")
    )

    checker = DoctorChecker(config, mock_shell)
    report = checker.run()

    run_check = next(c for c in report.checks if c.name == "my-app/task:run")
    assert run_check.status == "warn"
    assert "nonexistent" in run_check.message
    assert "aliased" in run_check.message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_doctor.py -v -k "alias"`
Expected: FAIL — doctor doesn't resolve aliases yet.

- [ ] **Step 3: Update `DoctorChecker.run` to resolve aliases**

In `src/mship/core/doctor.py`, replace the standard tasks loop (lines 63-70):

```python
            # Standard tasks (resolved through tasks mapping)
            result = self._shell.run("task --list", cwd=repo.path)
            if result.returncode == 0:
                task_output = result.stdout
                for canonical in ["test", "run", "lint", "setup"]:
                    actual = repo.tasks.get(canonical, canonical)
                    if actual in task_output:
                        msg = (
                            f"task '{actual}' available"
                            if actual == canonical
                            else f"task '{actual}' available (alias for '{canonical}')"
                        )
                        report.checks.append(CheckResult(
                            name=f"{name}/task:{canonical}",
                            status="pass",
                            message=msg,
                        ))
                    else:
                        msg = (
                            f"missing task: {actual}"
                            if actual == canonical
                            else f"missing task: {actual} (aliased from '{canonical}')"
                        )
                        report.checks.append(CheckResult(
                            name=f"{name}/task:{canonical}",
                            status="warn",
                            message=msg,
                        ))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_doctor.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/doctor.py tests/core/test_doctor.py
git commit -m "fix: doctor resolves tasks mapping aliases before checking Taskfile"
```

---

### Task 7: Documentation Updates

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add Monorepo section after Configuration**

In `README.md`, find the Configuration section. After the `env_runner` subsection (around line 197), add:

```markdown
### Monorepo Support (`git_root`)

For monorepos where multiple services share one git repo, use `git_root` to declare subdirectory services:

```yaml
repos:
  backend:
    path: .
    type: service
  web:
    path: web              # relative — interpreted against backend's worktree
    type: service
    git_root: backend
    depends_on: [backend]
```

The subdirectory service shares the parent's worktree. `mship spawn` creates one worktree for `backend` at `.worktrees/feat/<task>/`, and `web`'s effective path becomes `.worktrees/feat/<task>/web`.

Rules:
- `git_root` must reference another repo in the workspace
- The referenced repo cannot itself have `git_root` set (no chaining)
- The subdirectory must exist and contain a `Taskfile.yml`
- Subdirectory services still have their own `depends_on`, `tags`, `tasks`, and `start_mode`

### Service Start Modes (`start_mode`)

For long-running services (dev servers, databases), set `start_mode: background`:

```yaml
repos:
  infra:
    path: ./infra
    type: service
    start_mode: background     # mship run launches and moves on
  backend:
    path: ./backend
    type: service
    start_mode: background
    depends_on: [infra]
  amplify:
    path: ./amplify
    type: service
    # start_mode defaults to foreground
    depends_on: [infra]
```

With `start_mode: background`, `mship run` launches the service in a thread (via `subprocess.Popen`) and continues to the next dependency tier without waiting for exit. Background services keep running until Ctrl-C propagates SIGINT through go-task to their child processes.

`start_mode` only affects `mship run`. Tests and logs always run foreground.

### Task Name Aliasing

If your Taskfile uses different task names than mothership's defaults (`test`, `run`, `lint`, `setup`), add a `tasks:` mapping:

```yaml
repos:
  my-app:
    path: .
    type: service
    tasks:
      run: dev                 # mship run → task dev
      test: test:all           # mship test → task test:all
      lint: lint:all
      setup: infra:start
```

`mship doctor` respects the mapping when checking for standard tasks.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add monorepo, start_mode, and task aliasing sections to README"
```

---

### Task 8: Integration Test

**Files:**
- Create: `tests/test_monorepo_integration.py`

- [ ] **Step 1: Write the integration test**

`tests/test_monorepo_integration.py`:

```python
"""Integration test: monorepo with subdir service, start_mode, aliases."""
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def monorepo_workspace(tmp_path: Path):
    """Create a single-repo monorepo workspace with a subdir service."""
    root = tmp_path / "tailrd"
    root.mkdir()
    (root / "Taskfile.yml").write_text(
        "version: '3'\n"
        "tasks:\n"
        "  dev:\n"
        "    cmds:\n"
        "      - echo backend-dev\n"
        "  test:\n"
        "    cmds:\n"
        "      - echo backend-test\n"
    )
    web = root / "web"
    web.mkdir()
    (web / "Taskfile.yml").write_text(
        "version: '3'\n"
        "tasks:\n"
        "  dev:\n"
        "    cmds:\n"
        "      - echo web-dev\n"
        "  test:\n"
        "    cmds:\n"
        "      - echo web-test\n"
    )

    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=root, check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"},
    )

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: tailrd
repos:
  tailrd:
    path: ./tailrd
    type: service
    tasks:
      run: dev
    start_mode: background
  web:
    path: web
    type: service
    git_root: tailrd
    tasks:
      run: dev
    start_mode: background
    depends_on: [tailrd]
"""
    )

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mock_shell.build_command.side_effect = lambda cmd, env_runner=None: cmd
    container.shell.override(mock_shell)

    yield tmp_path, mock_shell

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def test_monorepo_spawn_shares_worktree(monorepo_workspace):
    tmp_path, mock_shell = monorepo_workspace

    result = runner.invoke(app, ["spawn", "add feature"])
    assert result.exit_code == 0, result.output

    mgr = StateManager(tmp_path / ".mothership")
    state = mgr.load()
    task = state.tasks["add-feature"]

    root_wt = Path(task.worktrees["tailrd"])
    web_wt = Path(task.worktrees["web"])

    # web's worktree is a subdirectory of tailrd's worktree
    assert web_wt == root_wt / "web"
    assert root_wt.exists()
    assert web_wt.exists()


def test_monorepo_abort_cleans_up(monorepo_workspace):
    tmp_path, mock_shell = monorepo_workspace

    runner.invoke(app, ["spawn", "cleanup test"])
    mgr = StateManager(tmp_path / ".mothership")
    state = mgr.load()
    root_wt = Path(state.tasks["cleanup-test"].worktrees["tailrd"])

    result = runner.invoke(app, ["abort", "--yes"])
    assert result.exit_code == 0

    # The parent worktree is removed
    assert not root_wt.exists()

    state = mgr.load()
    assert state.current_task is None
    assert "cleanup-test" not in state.tasks


def test_monorepo_run_uses_background(monorepo_workspace):
    """mship run should launch both services in background."""
    tmp_path, mock_shell = monorepo_workspace

    runner.invoke(app, ["spawn", "run test"])

    # Set up Popen mocks that exit immediately
    popen_mocks = []
    def make_popen(*args, **kwargs):
        p = MagicMock()
        p.pid = 10000 + len(popen_mocks)
        p.wait.return_value = 0
        p.poll.return_value = 0
        popen_mocks.append(p)
        return p
    mock_shell.run_streaming.side_effect = make_popen

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0

    # Both background services should have been launched
    assert mock_shell.run_streaming.call_count == 2
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_monorepo_integration.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 3: Run full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 4: Verify CLI help shows no changes**

Run: `uv run mship --help`
Expected: Same commands as before (no new CLI commands added; these are config changes).

- [ ] **Step 5: Commit**

```bash
git add tests/test_monorepo_integration.py
git commit -m "test: add integration test for monorepo workspace"
```

---

## Self-Review

**Spec coverage:**
- `git_root` field on RepoConfig: Task 1
- `git_root` validation (ref exists, no chaining): Task 1
- `git_root` subdirectory path validation at load: Task 1
- Worktree spawn skips `git_root` repos: Task 2
- Worktree effective path computed for `git_root`: Task 2
- Abort skips `git_root` repos: Task 2
- Executor `_resolve_cwd` chains through `git_root`: Task 3
- `start_mode` field with foreground/background: Task 1
- Background launches via `run_streaming`: Task 4
- Background Popen handles tracked in ExecutionResult: Task 4
- `mship run` waits on backgrounds with SIGINT forwarding: Task 5
- Doctor resolves `tasks:` mapping aliases: Task 6
- README docs for all three features: Task 7
- Integration test: Task 8

**Placeholder scan:** No TBDs. All code is complete.

**Type consistency:** `git_root: str | None`, `start_mode: Literal["foreground", "background"]`, `ExecutionResult.background_processes: list` (list of Popen). `_execute_one` returns `tuple[RepoResult, object | None]` (second element is Popen for background, None for foreground). All consistent across tasks.
