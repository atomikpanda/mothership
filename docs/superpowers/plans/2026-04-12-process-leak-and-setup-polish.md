# Process Leak Fix & Setup Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the background service grandchild process leak via process groups, and polish the spawn-time setup task (visibility, failure warnings, skip flag).

**Architecture:** `ShellRunner.run_streaming` launches with process group isolation (`start_new_session=True` on Unix, `CREATE_NEW_PROCESS_GROUP` on Windows). `mship run` signals the whole group via `os.killpg` / `CTRL_BREAK_EVENT`. `WorktreeManager.spawn` returns a `SpawnResult` with setup warnings and accepts a `skip_setup` flag.

**Tech Stack:** Python 3.14, `subprocess.Popen` with process-group flags, `os.killpg` (Unix), `signal.CTRL_BREAK_EVENT` (Windows)

---

## File Map

- `src/mship/util/shell.py` — `run_streaming` uses process group flags
- `src/mship/cli/exec.py` — `_kill_group` helper; group-signaled termination
- `src/mship/core/worktree.py` — `SpawnResult` dataclass; `spawn` accepts `skip_setup`, collects warnings
- `src/mship/cli/worktree.py` — `--skip-setup` flag; shows "Running setup..." and warnings
- Tests for each of the above

---

### Task 1: Process Group Flags in `run_streaming`

**Files:**
- Modify: `src/mship/util/shell.py`
- Modify: `tests/util/test_shell.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/util/test_shell.py`:

```python
from unittest.mock import patch


def test_run_streaming_uses_start_new_session_on_unix():
    """On Unix, run_streaming should pass start_new_session=True to Popen."""
    runner = ShellRunner()
    with patch("mship.util.shell.os.name", "posix"):
        with patch("subprocess.Popen") as mock_popen:
            runner.run_streaming("sleep 1", cwd=Path("."))
            kwargs = mock_popen.call_args.kwargs
            assert kwargs.get("start_new_session") is True
            assert "creationflags" not in kwargs


def test_run_streaming_uses_new_process_group_on_windows():
    """On Windows, run_streaming should pass creationflags=CREATE_NEW_PROCESS_GROUP."""
    runner = ShellRunner()
    with patch("mship.util.shell.os.name", "nt"):
        with patch("subprocess.Popen") as mock_popen:
            runner.run_streaming("sleep 1", cwd=Path("."))
            kwargs = mock_popen.call_args.kwargs
            assert kwargs.get("creationflags") == subprocess.CREATE_NEW_PROCESS_GROUP
            assert "start_new_session" not in kwargs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/util/test_shell.py -v -k "process_group or start_new_session or new_process_group"`
Expected: FAIL — the flags aren't set.

- [ ] **Step 3: Update `run_streaming` in `src/mship/util/shell.py`**

Add `import os` at the top (currently imported locally in `run`). Replace `run_streaming` with:

```python
    def run_streaming(self, command: str, cwd: Path) -> subprocess.Popen:
        """Run a command with stdout/stderr streaming (for logs, run).

        Launches the subprocess in its own process group so signal delivery
        can reach the whole tree (including grandchildren) on termination.
        """
        kwargs = dict(
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(command, **kwargs)
```

Move the `import os` to module top:

```python
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
```

And remove the `import os` from inside the `run` method.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/util/test_shell.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/util/shell.py tests/util/test_shell.py
git commit -m "fix: launch background processes in new session/process group for proper signal delivery"
```

---

### Task 2: Signal the Process Group in `mship run`

**Files:**
- Modify: `src/mship/cli/exec.py`
- Modify: `tests/cli/test_exec.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/cli/test_exec.py`:

```python
def test_mship_run_signals_process_group_on_failure(workspace: Path):
    """On launch failure, background processes get signaled via killpg (not terminate())."""
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
    start_mode: foreground
    depends_on: [shared]
"""
    )
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    cli_container.config_path.override(cfg)
    cli_container.state_dir.override(state_dir)

    mgr = StateManager(state_dir)
    task = Task(
        slug="grp-test",
        description="Group test",
        phase="dev",
        created_at=datetime(2026, 4, 12, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/grp-test",
    )
    mgr.save(WorkspaceState(current_task="grp-test", tasks={"grp-test": task}))

    # Mock shell: run_streaming succeeds, run_task fails on auth-service
    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 99999
    mock_shell.run_streaming.return_value = popen_mock
    mock_shell.build_command.return_value = "task run"
    mock_shell.run_task.return_value = ShellResult(returncode=1, stdout="", stderr="fail")
    cli_container.shell.override(mock_shell)

    with patch("mship.cli.exec.os") as mock_os:
        mock_os.name = "posix"
        result = runner.invoke(app, ["run"])

    # Background process should be killed via killpg, not terminate()
    assert mock_os.killpg.called
    popen_mock.terminate.assert_not_called()

    cli_container.config_path.reset_override()
    cli_container.state_dir.reset_override()
    cli_container.config.reset()
    cli_container.state_manager.reset()
    cli_container.shell.reset_override()
```

Ensure `from unittest.mock import patch` is imported at the top of the test file if not present.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_exec.py -v -k "signals_process_group"`
Expected: FAIL — current code uses `proc.terminate()`.

- [ ] **Step 3: Update `run_cmd` in `src/mship/cli/exec.py`**

Add `import os` at the top of the file (module-level, after the existing imports):

```python
import os
import signal
from typing import Optional

import typer

from mship.cli.output import Output
```

Then inside `run_cmd`, replace the termination logic. Add this helper at the top of `run_cmd` (after the `import signal` inside the function — or move signal to module top):

Keep `import signal` where it is (local is fine), and replace the whole termination handling. The new `run_cmd` body should look like:

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

        def _kill_group(proc, sig):
            """Send sig to the whole process group. Cross-platform."""
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(proc.pid, sig)
            except (ProcessLookupError, OSError):
                try:
                    proc.send_signal(sig)
                except Exception:
                    pass
            except Exception:
                pass

        if not result.success:
            for repo_result in result.results:
                if not repo_result.success:
                    output.error(f"{repo_result.repo}: failed to start")
            # Terminate any background processes that did start
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGINT)
            raise typer.Exit(code=1)

        if not result.background_processes:
            output.success("All services started")
            return

        # Have background services — wait for them with signal forwarding
        output.success(f"Started {len(result.background_processes)} background service(s). Press Ctrl-C to stop.")

        def _forward_sigint(signum, frame):
            for proc in result.background_processes:
                _kill_group(proc, signal.SIGINT)

        signal.signal(signal.SIGINT, _forward_sigint)

        try:
            for proc in result.background_processes:
                proc.wait()
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

        output.print("All background services have exited")
```

Key changes from current:
- `_kill_group` helper that uses `os.killpg` on Unix, `CTRL_BREAK_EVENT` on Windows
- Launch failure branch uses `_kill_group` instead of `proc.terminate()`
- SIGINT forward uses `_kill_group`
- KeyboardInterrupt cleanup uses `_kill_group` (both SIGINT and SIGKILL/SIGTERM)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_exec.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/exec.py tests/cli/test_exec.py
git commit -m "fix: signal process group for background service termination (catches grandchildren)"
```

---

### Task 3: `SpawnResult` Dataclass & `skip_setup` in WorktreeManager

**Files:**
- Modify: `src/mship/core/worktree.py`
- Modify: `tests/core/test_worktree.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_worktree.py`:

```python
def test_spawn_returns_spawn_result_with_task(worktree_deps):
    """spawn now returns SpawnResult, not Task."""
    from mship.core.worktree import SpawnResult
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    result = mgr.spawn("result test", repos=["shared"])
    assert isinstance(result, SpawnResult)
    assert result.task.slug == "result-test"
    assert result.setup_warnings == []


def test_spawn_collects_setup_warnings_on_failure(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    # Make setup return non-zero
    shell.run_task.return_value = ShellResult(
        returncode=1, stdout="", stderr="setup task not found"
    )
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    result = mgr.spawn("warning test", repos=["shared"])
    assert len(result.setup_warnings) == 1
    assert "shared" in result.setup_warnings[0]
    assert "setup" in result.setup_warnings[0].lower()


def test_spawn_skip_setup_does_not_call_setup(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("skip test", repos=["shared"], skip_setup=True)
    # run_task should not have been called (no setup ran)
    shell.run_task.assert_not_called()
```

**Also update existing tests:** the `spawn` method now returns `SpawnResult` instead of `Task`. Find existing tests in `test_worktree.py` that do `mgr.spawn(...)` and use the return value. The common pattern is:

```python
mgr.spawn("desc", repos=["shared"])
state = state_mgr.load()
task = state.tasks["desc-slug"]
```

That still works (reads from state). But if any test does:
```python
task = mgr.spawn("desc")
```
...change to:
```python
result = mgr.spawn("desc")
task = result.task
```

Check the file for patterns like `= mgr.spawn(` and adjust.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_worktree.py -v -k "spawn_returns_spawn_result or spawn_collects_setup or spawn_skip_setup"`
Expected: FAIL — `SpawnResult` doesn't exist, `skip_setup` isn't a parameter.

- [ ] **Step 3: Update `src/mship/core/worktree.py`**

Add `dataclass` import and `SpawnResult` class at the top of the file:

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mship.core.config import WorkspaceConfig
from mship.core.graph import DependencyGraph
from mship.core.log import LogManager
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.git import GitRunner
from mship.util.shell import ShellRunner
from mship.util.slug import slugify


@dataclass
class SpawnResult:
    task: Task
    setup_warnings: list[str] = field(default_factory=list)
```

Replace the `spawn` method with:

```python
    def spawn(
        self,
        description: str,
        repos: list[str] | None = None,
        skip_setup: bool = False,
    ) -> SpawnResult:
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
        setup_warnings: list[str] = []

        for repo_name in ordered:
            repo_config = self._config.repos[repo_name]

            if repo_config.git_root is not None:
                # Subdirectory service: share parent's worktree
                parent_wt = worktrees.get(repo_config.git_root)
                if parent_wt is None:
                    parent_wt = self._config.repos[repo_config.git_root].path
                effective = parent_wt / repo_config.path
                worktrees[repo_name] = effective

                if not skip_setup:
                    actual_setup = repo_config.tasks.get("setup", "setup")
                    setup_result = self._shell.run_task(
                        task_name="setup",
                        actual_task_name=actual_setup,
                        cwd=effective,
                        env_runner=repo_config.env_runner or self._config.env_runner,
                    )
                    if setup_result.returncode != 0:
                        setup_warnings.append(
                            f"{repo_name}: setup failed (task '{actual_setup}') — "
                            f"{setup_result.stderr.strip()[:200]}"
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

            if not skip_setup:
                actual_setup = repo_config.tasks.get("setup", "setup")
                setup_result = self._shell.run_task(
                    task_name="setup",
                    actual_task_name=actual_setup,
                    cwd=wt_path,
                    env_runner=repo_config.env_runner or self._config.env_runner,
                )
                if setup_result.returncode != 0:
                    setup_warnings.append(
                        f"{repo_name}: setup failed (task '{actual_setup}') — "
                        f"{setup_result.stderr.strip()[:200]}"
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
        log_msg = f"Task spawned. Repos: {', '.join(ordered)}. Branch: {branch}"
        if skip_setup:
            log_msg += " (setup skipped)"
        self._log.append(slug, log_msg)

        return SpawnResult(task=task, setup_warnings=setup_warnings)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_worktree.py -v`
Expected: All tests PASS (existing + new). If any existing tests reference the old return type, they need updating — see step 1.

- [ ] **Step 5: Full suite**

Run: `uv run pytest tests/ -q`
Expected: Tests in `tests/cli/test_worktree.py` and integration tests may fail if they use `mgr.spawn(...)` return value as a Task. Fix the CLI first (Task 4).

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat: WorktreeManager.spawn returns SpawnResult with setup warnings and accepts skip_setup"
```

---

### Task 4: CLI `--skip-setup` Flag & Setup Output

**Files:**
- Modify: `src/mship/cli/worktree.py`
- Modify: `tests/cli/test_worktree.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/cli/test_worktree.py`:

```python
def test_spawn_skip_setup_flag(configured_git_app: Path):
    """--skip-setup should skip the setup task."""
    from mship.cli import container as cli_container
    from unittest.mock import MagicMock
    from mship.util.shell import ShellResult, ShellRunner

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["spawn", "skip flag test", "--repos", "shared", "--skip-setup"])
    assert result.exit_code == 0, result.output
    # run_task should not have been called for setup
    mock_shell.run_task.assert_not_called()

    cli_container.shell.reset_override()


def test_spawn_shows_setup_warnings(configured_git_app: Path):
    """Setup failures should appear as warnings in output."""
    from mship.cli import container as cli_container
    from unittest.mock import MagicMock
    from mship.util.shell import ShellResult, ShellRunner

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(
        returncode=1, stdout="", stderr="pnpm install failed"
    )
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["spawn", "warning flag test", "--repos", "shared"])
    assert result.exit_code == 0, result.output
    # Setup failure should appear in output as a warning
    assert "setup failed" in result.output.lower() or "pnpm install failed" in result.output

    cli_container.shell.reset_override()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_worktree.py -v -k "skip_setup or setup_warnings"`
Expected: FAIL — flag doesn't exist, warnings aren't shown.

- [ ] **Step 3: Update `spawn` command in `src/mship/cli/worktree.py`**

Replace the `spawn` command. Here's the existing code (for reference):

```python
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
```

Replace with:

```python
    @app.command()
    def spawn(
        description: str,
        repos: Optional[str] = typer.Option(None, help="Comma-separated repo names"),
        skip_setup: bool = typer.Option(False, "--skip-setup", help="Skip running `task setup` in new worktrees"),
    ):
        """Create coordinated worktrees across repos for a new task."""
        container = get_container()
        output = Output()
        wt_mgr = container.worktree_manager()

        repo_list = repos.split(",") if repos else None

        if output.is_tty and not skip_setup:
            output.print("[dim]Running setup in each worktree (use --skip-setup to skip)...[/dim]")

        result = wt_mgr.spawn(description, repos=repo_list, skip_setup=skip_setup)
        task = result.task

        if output.is_tty:
            output.success(f"Spawned task: {task.slug}")
            output.print(f"  Branch: {task.branch}")
            output.print(f"  Phase: {task.phase}")
            output.print(f"  Repos: {', '.join(task.affected_repos)}")
            for repo, path in task.worktrees.items():
                output.print(f"  {repo}: {path}")
            for warning in result.setup_warnings:
                output.warning(warning)
        else:
            data = task.model_dump(mode="json")
            data["setup_warnings"] = result.setup_warnings
            output.json(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_worktree.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Full suite**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS. If any integration tests fail because they call `wt_mgr.spawn(...)` and expect a Task return, update them to use `result.task`.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_worktree.py
git commit -m "feat: mship spawn shows setup warnings and accepts --skip-setup flag"
```

---

### Task 5: Integration Test (Process Leak Scenario)

**Files:**
- Modify: `tests/test_monorepo_integration.py`

- [ ] **Step 1: Write the test**

Add to `tests/test_monorepo_integration.py`:

```python
def test_monorepo_run_uses_process_group(monorepo_workspace):
    """Background services should be launched in their own process group."""
    from unittest.mock import patch
    tmp_path, mock_shell = monorepo_workspace

    runner.invoke(app, ["spawn", "group test", "--skip-setup"])

    popen_mocks = []
    def make_popen(*args, **kwargs):
        p = MagicMock()
        p.pid = 50000 + len(popen_mocks)
        p.wait.return_value = 0
        p.poll.return_value = 0
        popen_mocks.append(p)
        return p
    mock_shell.run_streaming.side_effect = make_popen

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0
    # Both repos launched in background
    assert mock_shell.run_streaming.call_count == 2
```

Note: this test doesn't verify that `start_new_session=True` is actually passed to Popen (that's tested in `test_shell.py`). It verifies the end-to-end flow still works with the new process-group code path.

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/test_monorepo_integration.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Full suite + CLI help**

Run: `uv run pytest tests/ -q`
Expected: All tests PASS.

Run: `uv run mship spawn --help`
Expected: Shows `--skip-setup` flag.

- [ ] **Step 4: Commit**

```bash
git add tests/test_monorepo_integration.py
git commit -m "test: add integration test for background service process group launch"
```

---

## Self-Review

**Spec coverage:**
- Process group on Unix (`start_new_session=True`): Task 1
- Process group on Windows (`CREATE_NEW_PROCESS_GROUP`): Task 1
- `os.killpg` / `CTRL_BREAK_EVENT` on termination: Task 2
- `SpawnResult` dataclass with `setup_warnings`: Task 3
- `skip_setup` parameter in core: Task 3
- `--skip-setup` CLI flag: Task 4
- "Running setup..." visibility message: Task 4
- Setup failures become warnings (not hard failures): Task 3 + Task 4 output
- Integration coverage: Task 5

**Placeholder scan:** No TBDs. All code is complete.

**Type consistency:** `SpawnResult` has `.task: Task` and `.setup_warnings: list[str]`. `skip_setup: bool` is consistent between core and CLI. `_kill_group(proc, sig)` signature is consistent across all call sites.
