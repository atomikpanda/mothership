# Real-World Bugs Round 2 & symlink_dirs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the background process leak (grandchildren surviving child exit), fix state dir resolution when CWD is inside a worktree, fix go-task parse errors in the starter Taskfile + doctor, add `symlink_dirs` feature, and print a startup summary for `mship run`.

**Architecture:** Mostly small, independent fixes to existing modules. `symlink_dirs` adds a new method to `WorktreeManager`. State dir anchoring uses `git rev-parse --git-common-dir`.

**Tech Stack:** Python 3.14, existing deps (no new packages)

---

## File Map

- `src/mship/cli/exec.py` — kill group after each `proc.wait()`; new startup summary with PIDs
- `src/mship/cli/__init__.py` — `_resolve_state_dir()` helper using git common dir
- `src/mship/core/init.py` — fix `TASKFILE_TEMPLATE` colons
- `src/mship/core/doctor.py` — fail check for `task --list` parse errors
- `src/mship/core/config.py` — add `symlink_dirs: list[str]` to `RepoConfig`
- `src/mship/core/worktree.py` — `_create_symlinks` method; call it before setup
- `src/mship/core/executor.py` — `background_pid: int | None` on `RepoResult`; set it in `_execute_one`

---

### Task 1: Process Group Cleanup After Child Exit

**Files:**
- Modify: `src/mship/cli/exec.py`
- Modify: `tests/cli/test_exec.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/cli/test_exec.py`:

```python
def test_mship_run_kills_group_after_child_exits(workspace: Path):
    """After proc.wait() returns, the process group should be signaled to catch grandchildren."""
    from mship.cli import container as cli_container
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
    cli_container.config_path.override(cfg)
    cli_container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="cleanup-test",
        description="Cleanup test",
        phase="dev",
        created_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/cleanup-test",
    )
    mgr.save(WorkspaceState(current_task="cleanup-test", tasks={"cleanup-test": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 77777
    popen_mock.wait.return_value = 0
    popen_mock.poll.return_value = 0
    mock_shell.run_streaming.return_value = popen_mock
    mock_shell.build_command.return_value = "task run"
    cli_container.shell.override(mock_shell)

    with patch("mship.cli.exec.os") as mock_os:
        mock_os.name = "posix"
        result = runner.invoke(app, ["run"])

    assert result.exit_code == 0
    # killpg should have been called to catch grandchildren
    assert mock_os.killpg.called
    # Should have been called multiple times (SIGTERM then SIGKILL)
    assert mock_os.killpg.call_count >= 2

    cli_container.config_path.reset_override()
    cli_container.state_dir.reset_override()
    cli_container.config.reset()
    cli_container.state_manager.reset()
    cli_container.shell.reset_override()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/cli/test_exec.py -v -k "kills_group_after"`
Expected: FAIL — current code doesn't call `killpg` after `wait()`.

- [ ] **Step 3: Update `run_cmd` in `src/mship/cli/exec.py`**

Find the successful-background-wait block in `run_cmd` (currently the last `try:` before the "All background services have exited" message):

```python
        try:
            for proc in result.background_processes:
                proc.wait()
        except KeyboardInterrupt:
            ...
```

Replace with:

```python
        try:
            for proc in result.background_processes:
                proc.wait()
                # Catch any surviving grandchildren in the process group
                _kill_group(proc, signal.SIGTERM)
            # Brief grace period, then SIGKILL stragglers
            import time
            time.sleep(0.5)
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
        except KeyboardInterrupt:
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGINT)
            for proc in result.background_processes:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    _kill_group(proc, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_exec.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/exec.py tests/cli/test_exec.py
git commit -m "fix: kill process group after background service exits to reap grandchildren"
```

---

### Task 2: State Dir Anchored to Git Common Dir

**Files:**
- Modify: `src/mship/cli/__init__.py`
- Create: `tests/cli/test_state_dir_resolution.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_state_dir_resolution.py`:

```python
import os
import subprocess
from pathlib import Path

import pytest


def test_resolve_state_dir_in_main_repo(tmp_path: Path):
    """In a plain git repo, state dir is <repo>/.mothership."""
    from mship.cli import _resolve_state_dir

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    config_path = tmp_path / "mothership.yaml"
    config_path.write_text("workspace: t\nrepos: {}\n")

    state_dir = _resolve_state_dir(config_path)
    assert state_dir == tmp_path / ".mothership"


def test_resolve_state_dir_in_worktree(tmp_path: Path):
    """From inside a worktree, state dir anchors to the MAIN repo, not the worktree."""
    from mship.cli import _resolve_state_dir

    main = tmp_path / "main"
    main.mkdir()
    (main / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")

    git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"}
    subprocess.run(["git", "init", str(main)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=main, check=True, capture_output=True, env=git_env)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=main, check=True, capture_output=True, env=git_env,
    )

    # Create a worktree
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(wt)],
        cwd=main, check=True, capture_output=True, env=git_env,
    )

    # Config in the worktree (checked out from git)
    wt_config = wt / "mothership.yaml"
    assert wt_config.exists()

    # From main: state dir is main/.mothership
    assert _resolve_state_dir(main / "mothership.yaml") == main / ".mothership"

    # From worktree: state dir STILL anchored to main
    assert _resolve_state_dir(wt_config) == main / ".mothership"


def test_resolve_state_dir_not_a_git_repo(tmp_path: Path):
    """If the directory is not a git repo, fall back to config path parent."""
    from mship.cli import _resolve_state_dir

    config_path = tmp_path / "mothership.yaml"
    config_path.write_text("workspace: t\nrepos: {}\n")

    state_dir = _resolve_state_dir(config_path)
    assert state_dir == tmp_path / ".mothership"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_state_dir_resolution.py -v`
Expected: FAIL — `_resolve_state_dir` doesn't exist.

- [ ] **Step 3: Add `_resolve_state_dir` to `src/mship/cli/__init__.py`**

At the top of the file, add the helper function (before `get_container`):

```python
def _resolve_state_dir(config_path):
    """Get the workspace state dir, anchored to main repo if in a git worktree."""
    import subprocess
    from pathlib import Path

    config_path = Path(config_path)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=config_path.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        git_common_dir = Path(result.stdout.strip())
        if not git_common_dir.is_absolute():
            git_common_dir = (config_path.parent / git_common_dir).resolve()
        return git_common_dir.parent / ".mothership"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return config_path.parent / ".mothership"
```

Update `get_container` to use it. Find this block:

```python
        if not container.state_dir.overridden:
            config_path = container.config_path()
            state_dir = Path(config_path).parent / ".mothership"
            container.state_dir.override(state_dir)
```

Replace with:

```python
        if not container.state_dir.overridden:
            config_path = container.config_path()
            state_dir = _resolve_state_dir(config_path)
            container.state_dir.override(state_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_state_dir_resolution.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/__init__.py tests/cli/test_state_dir_resolution.py
git commit -m "fix: anchor state dir to git common dir so mship status works from worktrees"
```

---

### Task 3: Taskfile Template + Doctor Parse Check

**Files:**
- Modify: `src/mship/core/init.py`
- Modify: `src/mship/core/doctor.py`
- Modify: `tests/core/test_init.py`
- Modify: `tests/core/test_doctor.py`

- [ ] **Step 1: Fix the `TASKFILE_TEMPLATE` in `src/mship/core/init.py`**

Find the `TASKFILE_TEMPLATE` class attribute on `WorkspaceInitializer`. It currently has four stub tasks with `echo "TODO: ..."`. Replace all four colons with dashes:

```python
    TASKFILE_TEMPLATE = """\
version: '3'

tasks:
  test:
    desc: Run tests
    cmds:
      - echo "TODO - add test command"

  run:
    desc: Start the service
    cmds:
      - echo "TODO - add run command"

  lint:
    desc: Run linter
    cmds:
      - echo "TODO - add lint command"

  setup:
    desc: Set up development environment
    cmds:
      - echo "TODO - add setup command"
"""
```

- [ ] **Step 2: Write a test for the template fix**

Add to `tests/core/test_init.py`:

```python
def test_taskfile_template_has_no_colon_in_echo_strings(tmp_path: Path):
    """go-task 3.49.1 rejects colons inside echo strings. Template must not use them."""
    repo = tmp_path / "my-repo"
    repo.mkdir()
    init = WorkspaceInitializer()
    init.write_taskfile(repo)
    content = (repo / "Taskfile.yml").read_text()
    # Each echo line should not contain a colon inside the quoted string
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- echo"):
            # Extract the quoted part
            assert "TODO:" not in stripped, f"Colon found in echo: {stripped}"
```

- [ ] **Step 3: Run test to verify it passes**

Run: `uv run pytest tests/core/test_init.py -v -k "no_colon"`
Expected: PASS.

- [ ] **Step 4: Update `DoctorChecker` to fail on Taskfile parse errors**

In `src/mship/core/doctor.py`, find the "Standard tasks" block in `run()`:

```python
            # Standard tasks (resolved through tasks mapping)
            result = self._shell.run("task --list", cwd=repo.path)
            if result.returncode == 0:
                task_output = result.stdout
                for canonical in ["test", "run", "lint", "setup"]:
                    ...
```

Replace the `if result.returncode == 0:` conditional with a returncode != 0 branch that reports a fail check, then continues:

```python
            # Standard tasks (resolved through tasks mapping)
            result = self._shell.run("task --list", cwd=repo.path)
            if result.returncode != 0:
                err_summary = (
                    result.stderr.strip()[:200]
                    if result.stderr
                    else "unknown error"
                )
                report.checks.append(CheckResult(
                    name=f"{name}/taskfile_parse",
                    status="fail",
                    message=f"Taskfile parse error: {err_summary}",
                ))
                continue  # skip the per-task checks for this repo
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

- [ ] **Step 5: Write a test for doctor parse failure**

Add to `tests/core/test_doctor.py`:

```python
def test_doctor_reports_taskfile_parse_error(tmp_path: Path):
    """When `task --list` returns non-zero, doctor emits a fail check."""
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
"""
    )
    config = ConfigLoader.load(cfg)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = lambda cmd, cwd, env=None: (
        ShellResult(returncode=1, stdout="", stderr="err: invalid keys in command\nfile: Taskfile.yml:7:9")
        if "task --list" in cmd
        else ShellResult(returncode=0, stdout="Logged in", stderr="")
    )

    checker = DoctorChecker(config, mock_shell)
    report = checker.run()

    parse_check = next(
        (c for c in report.checks if c.name == "my-app/taskfile_parse"),
        None,
    )
    assert parse_check is not None
    assert parse_check.status == "fail"
    assert "parse" in parse_check.message.lower() or "invalid keys" in parse_check.message

    # Per-task checks should NOT have been emitted (we skip after parse fail)
    task_checks = [c for c in report.checks if "my-app/task:" in c.name]
    assert task_checks == []
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/core/test_doctor.py tests/core/test_init.py -v`
Expected: All tests PASS.

- [ ] **Step 7: Full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
git add src/mship/core/init.py src/mship/core/doctor.py tests/core/test_init.py tests/core/test_doctor.py
git commit -m "fix: remove colons from TASKFILE_TEMPLATE; doctor reports Taskfile parse errors"
```

---

### Task 4: symlink_dirs Config and Implementation

**Files:**
- Modify: `src/mship/core/config.py`
- Modify: `src/mship/core/worktree.py`
- Modify: `tests/core/test_config.py`
- Modify: `tests/core/test_worktree.py`

- [ ] **Step 1: Add `symlink_dirs` to `RepoConfig`**

In `src/mship/core/config.py`, add to `RepoConfig`:

```python
    symlink_dirs: list[str] = []
```

- [ ] **Step 2: Write config test**

Add to `tests/core/test_config.py`:

```python
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
```

- [ ] **Step 3: Run config test to verify it passes**

Run: `uv run pytest tests/core/test_config.py -v -k "symlink"`
Expected: PASS.

- [ ] **Step 4: Add `_create_symlinks` to `WorktreeManager`**

In `src/mship/core/worktree.py`, add this method to the class:

```python
    def _create_symlinks(
        self,
        repo_name: str,
        repo_config,
        worktree_path: Path,
    ) -> list[str]:
        """Create symlinks from source repo into the worktree. Returns warnings."""
        warnings: list[str] = []
        if not repo_config.symlink_dirs:
            return warnings

        if repo_config.git_root is not None:
            parent = self._config.repos[repo_config.git_root]
            source_root = parent.path / repo_config.path
        else:
            source_root = repo_config.path

        for dir_name in repo_config.symlink_dirs:
            source = source_root / dir_name
            target = worktree_path / dir_name

            if not source.exists():
                warnings.append(
                    f"{repo_name}: symlink source missing: {dir_name} (will not be linked)"
                )
                continue

            if target.exists() and not target.is_symlink():
                warnings.append(
                    f"{repo_name}: symlink skipped, {dir_name} already exists as a real directory"
                )
                continue

            if target.is_symlink():
                target.unlink()

            target.symlink_to(source.resolve())

        return warnings
```

- [ ] **Step 5: Call `_create_symlinks` in `spawn`**

In `WorktreeManager.spawn`, there are two places where a repo gets its worktree path set — the `git_root` branch and the normal branch. After setting `worktrees[repo_name] = ...` and BEFORE the `if not skip_setup:` block, add the symlink call.

**In the `git_root` branch** (after `worktrees[repo_name] = effective`):

```python
                worktrees[repo_name] = effective

                # Create symlinks before setup so setup can use the linked dirs
                symlink_warnings = self._create_symlinks(repo_name, repo_config, effective)
                setup_warnings.extend(symlink_warnings)

                if not skip_setup:
                    # existing setup block
                    ...
```

**In the normal branch** (after `worktrees[repo_name] = wt_path`):

```python
            worktrees[repo_name] = wt_path

            # Create symlinks before setup so setup can use the linked dirs
            symlink_warnings = self._create_symlinks(repo_name, repo_config, wt_path)
            setup_warnings.extend(symlink_warnings)

            if not skip_setup:
                # existing setup block
                ...
```

- [ ] **Step 6: Write tests for `_create_symlinks`**

Add to `tests/core/test_worktree.py`:

```python
def test_create_symlinks_creates_symlink_when_source_exists(tmp_path: Path):
    """When source exists and target doesn't, create the symlink."""
    import os
    import subprocess

    # Set up a repo with a node_modules directory
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Taskfile.yml").write_text("version: '3'")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "some_pkg").mkdir()

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  repo:
    path: ./repo
    type: service
    symlink_dirs: [node_modules]
"""
    )
    from mship.core.config import ConfigLoader
    from mship.core.graph import DependencyGraph
    from mship.core.state import StateManager
    from mship.core.log import LogManager
    from mship.util.git import GitRunner
    from mship.util.shell import ShellRunner

    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    log = MagicMock(spec=LogManager)

    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)

    wt = tmp_path / "wt"
    wt.mkdir()
    warnings = mgr._create_symlinks("repo", config.repos["repo"], wt)

    assert warnings == []
    symlink = wt / "node_modules"
    assert symlink.is_symlink()
    assert symlink.resolve() == (repo / "node_modules").resolve()


def test_create_symlinks_warns_when_source_missing(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Taskfile.yml").write_text("version: '3'")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  repo:
    path: ./repo
    type: service
    symlink_dirs: [node_modules]
"""
    )
    from mship.core.config import ConfigLoader
    from mship.core.graph import DependencyGraph
    from mship.core.state import StateManager
    from mship.core.log import LogManager
    from mship.util.git import GitRunner
    from mship.util.shell import ShellRunner

    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    log = MagicMock(spec=LogManager)

    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)

    wt = tmp_path / "wt"
    wt.mkdir()
    warnings = mgr._create_symlinks("repo", config.repos["repo"], wt)

    assert len(warnings) == 1
    assert "source missing" in warnings[0]
    assert "node_modules" in warnings[0]
    assert not (wt / "node_modules").exists()


def test_create_symlinks_skips_when_target_is_real_dir(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Taskfile.yml").write_text("version: '3'")
    (repo / "node_modules").mkdir()

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  repo:
    path: ./repo
    type: service
    symlink_dirs: [node_modules]
"""
    )
    from mship.core.config import ConfigLoader
    from mship.core.graph import DependencyGraph
    from mship.core.state import StateManager
    from mship.core.log import LogManager
    from mship.util.git import GitRunner
    from mship.util.shell import ShellRunner

    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    log = MagicMock(spec=LogManager)

    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)

    wt = tmp_path / "wt"
    wt.mkdir()
    # Create a real directory in the worktree path
    (wt / "node_modules").mkdir()

    warnings = mgr._create_symlinks("repo", config.repos["repo"], wt)

    assert len(warnings) == 1
    assert "already exists as a real directory" in warnings[0]
    # Target should still be a real dir, not a symlink
    assert not (wt / "node_modules").is_symlink()


def test_create_symlinks_replaces_stale_symlink(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Taskfile.yml").write_text("version: '3'")
    (repo / "node_modules").mkdir()

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  repo:
    path: ./repo
    type: service
    symlink_dirs: [node_modules]
"""
    )
    from mship.core.config import ConfigLoader
    from mship.core.graph import DependencyGraph
    from mship.core.state import StateManager
    from mship.core.log import LogManager
    from mship.util.git import GitRunner
    from mship.util.shell import ShellRunner

    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    log = MagicMock(spec=LogManager)

    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)

    wt = tmp_path / "wt"
    wt.mkdir()
    # Create a stale symlink pointing at a nonexistent location
    stale_target = tmp_path / "nonexistent"
    (wt / "node_modules").symlink_to(stale_target)

    warnings = mgr._create_symlinks("repo", config.repos["repo"], wt)

    assert warnings == []
    assert (wt / "node_modules").is_symlink()
    # Now points at the real source
    assert (wt / "node_modules").resolve() == (repo / "node_modules").resolve()
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_worktree.py -v -k "symlink"`
Expected: All 4 tests PASS.

- [ ] **Step 8: Full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 9: Commit**

```bash
git add src/mship/core/config.py src/mship/core/worktree.py tests/core/test_config.py tests/core/test_worktree.py
git commit -m "feat: add symlink_dirs config for per-worktree node_modules / .venv / etc. symlinks"
```

---

### Task 5: Startup Summary with PIDs

**Files:**
- Modify: `src/mship/core/executor.py`
- Modify: `src/mship/cli/exec.py`
- Modify: `tests/core/test_executor.py`
- Modify: `tests/cli/test_exec.py`

- [ ] **Step 1: Add `background_pid` to `RepoResult`**

In `src/mship/core/executor.py`:

```python
@dataclass
class RepoResult:
    repo: str
    task_name: str
    shell_result: ShellResult
    skipped: bool = False
    background_pid: int | None = None

    @property
    def success(self) -> bool:
        return self.shell_result.returncode == 0 if not self.skipped else True
```

- [ ] **Step 2: Set `background_pid` in `_execute_one`**

Find the background branch in `_execute_one`:

```python
        if repo_config.start_mode == "background" and canonical_task == "run":
            command = self._shell.build_command(
                f"task {actual_name}", env_runner
            )
            popen = self._shell.run_streaming(command, cwd=cwd)
            return (
                RepoResult(
                    repo=repo_name,
                    task_name=actual_name,
                    shell_result=ShellResult(returncode=0, stdout="", stderr=""),
                ),
                popen,
            )
```

Add `background_pid=popen.pid`:

```python
        if repo_config.start_mode == "background" and canonical_task == "run":
            command = self._shell.build_command(
                f"task {actual_name}", env_runner
            )
            popen = self._shell.run_streaming(command, cwd=cwd)
            return (
                RepoResult(
                    repo=repo_name,
                    task_name=actual_name,
                    shell_result=ShellResult(returncode=0, stdout="", stderr=""),
                    background_pid=popen.pid,
                ),
                popen,
            )
```

- [ ] **Step 3: Write executor tests**

Add to `tests/core/test_executor.py`:

```python
def test_background_repo_result_has_pid(workspace: Path):
    """RepoResult.background_pid is set for background launches."""
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
    popen_mock.pid = 55555
    mock_shell.run_streaming.return_value = popen_mock

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute("run", repos=["shared"])
    assert result.results[0].background_pid == 55555


def test_foreground_repo_result_has_no_pid(workspace: Path):
    """RepoResult.background_pid is None for foreground tasks."""
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute("test", repos=["shared"])
    assert result.results[0].background_pid is None
```

- [ ] **Step 4: Update `run_cmd` output in `src/mship/cli/exec.py`**

Find the block after the "no backgrounds" early-return:

```python
        # Have background services — wait for them with signal forwarding
        output.success(f"Started {len(result.background_processes)} background service(s). Press Ctrl-C to stop.")
```

Replace with:

```python
        # Have background services — wait for them with signal forwarding
        output.success(f"Started {len(result.background_processes)} background service(s):")
        for repo_result in result.results:
            if repo_result.background_pid is not None:
                output.print(
                    f"  [green]✓[/green] {repo_result.repo} → task {repo_result.task_name}  (pid {repo_result.background_pid})"
                )
        output.print("")
        output.print("Press Ctrl-C to stop.")
```

- [ ] **Step 5: Write CLI test**

Add to `tests/cli/test_exec.py`:

```python
def test_mship_run_shows_startup_summary_with_pids(workspace: Path):
    """Startup summary should list each background service with its PID."""
    from mship.cli import container as cli_container
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
  auth-service:
    path: ./auth-service
    type: service
    start_mode: background
    depends_on: [shared]
"""
    )
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    cli_container.config_path.override(cfg)
    cli_container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="summary-test",
        description="Summary test",
        phase="dev",
        created_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/summary-test",
    )
    mgr.save(WorkspaceState(current_task="summary-test", tasks={"summary-test": task}))

    mock_shell = MagicMock(spec=ShellRunner)
    pids = [11111, 22222]
    popen_mocks = []

    def make_popen(*args, **kwargs):
        p = MagicMock()
        p.pid = pids[len(popen_mocks)]
        p.wait.return_value = 0
        p.poll.return_value = 0
        popen_mocks.append(p)
        return p

    mock_shell.run_streaming.side_effect = make_popen
    mock_shell.build_command.return_value = "task run"
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    assert "11111" in result.output
    assert "22222" in result.output
    assert "shared" in result.output
    assert "auth-service" in result.output

    cli_container.config_path.reset_override()
    cli_container.state_dir.reset_override()
    cli_container.config.reset()
    cli_container.state_manager.reset()
    cli_container.shell.reset_override()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_executor.py tests/cli/test_exec.py -v`
Expected: All tests PASS.

- [ ] **Step 7: Full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
git add src/mship/core/executor.py src/mship/cli/exec.py tests/core/test_executor.py tests/cli/test_exec.py
git commit -m "feat: mship run prints startup summary with repo/task/PID for background services"
```

---

## Self-Review

**Spec coverage:**
- Bug 1 (process leak): Task 1
- Bug 2 (state dir): Task 2
- Bug 3a (TASKFILE_TEMPLATE): Task 3
- Bug 3b (doctor parse check): Task 3
- Feature 4 (symlink_dirs): Task 4
- Minor 5 (startup summary): Task 5

**Placeholder scan:** No TBDs. Every step has exact code.

**Type consistency:** `background_pid: int | None` is consistent across `RepoResult`, `_execute_one`, test assertions, and CLI output access (`repo_result.background_pid`). `_create_symlinks` returns `list[str]`, called from both branches in `spawn`, warnings merged into `setup_warnings`.
