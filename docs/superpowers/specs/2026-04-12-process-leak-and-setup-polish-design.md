# Process Leak Fix & Setup Polish Design Spec

## Overview

Two narrow fixes for real-world mothership usage:

1. **Process leak in `mship run`** — background services' grandchild processes survive Ctrl-C because we only signal the direct child PID, not the whole process group. A `task dev` that exits early leaves its `uvicorn` grandchild holding port 8000.
2. **Setup task UX** — `mship spawn` already runs `task setup` in each new worktree, but execution is silent, failures are swallowed, and there's no way to skip it.

## 1. Process Group Handling for Background Services

### Problem

`ShellRunner.run_streaming()` returns a `Popen` for the direct child process (`task dev`). When the user hits Ctrl-C, `mship run` sends SIGINT to that PID. But if `task dev` has already exited (e.g., failed early) while spawning `uvicorn` as its grandchild, the SIGINT goes nowhere and `uvicorn` keeps running, holding its port.

### Solution

Spawn background processes in their own process group, then signal the whole group on termination.

**Unix (`start_new_session=True`):**
```python
proc = subprocess.Popen(..., start_new_session=True)
# Later:
os.killpg(proc.pid, signal.SIGINT)   # signals the whole group
```

**Windows (`creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`):**
```python
proc = subprocess.Popen(..., creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
# Later:
proc.send_signal(signal.CTRL_BREAK_EVENT)
```

### Changes

**`src/mship/util/shell.py` — `run_streaming`:**

Replace the current implementation with one that sets up the process group:

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

**`src/mship/cli/exec.py` — `run_cmd`:**

Replace the `_forward_sigint` handler and the `KeyboardInterrupt` cleanup to signal the whole process group:

```python
def _kill_group(proc, sig):
    """Send sig to the whole process group of proc. Cross-platform."""
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, sig)
    except (ProcessLookupError, OSError):
        # Group already gone — try direct PID as fallback
        try:
            proc.send_signal(sig)
        except Exception:
            pass

def _forward_sigint(signum, frame):
    for proc in result.background_processes:
        _kill_group(proc, signal.SIGINT)
```

And in the `KeyboardInterrupt` branch, use `_kill_group` for both the SIGINT forward and the final SIGKILL:

```python
except KeyboardInterrupt:
    for proc in result.background_processes:
        _kill_group(proc, signal.SIGINT)
    for proc in result.background_processes:
        try:
            proc.wait(timeout=5)
        except Exception:
            _kill_group(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
```

The terminate-on-launch-failure branch in `run_cmd` also uses `_kill_group` instead of `proc.terminate()`.

### Test Strategy

Can't easily test actual process group signaling in pytest (requires real fork/grandchildren). Instead:

- Mock `subprocess.Popen` and verify `start_new_session=True` / `creationflags=CREATE_NEW_PROCESS_GROUP` is passed
- Mock `os.killpg` and verify it's called with the pid and signal
- Existing `test_mship_run_waits_for_background_services` continues to pass

## 2. Setup Task UX Polish

### Problem

`mship spawn` already runs `task setup` in each worktree after creating it, but:

1. The output says nothing — users think `spawn` is hanging during slow installs
2. Setup failures are ignored — no feedback if `pnpm install` fails or the `setup` task doesn't exist
3. No way to skip setup when you know deps are fresh

### Solution

Make setup visible, surface failures as warnings, add `--skip-setup` flag.

### Changes

**`src/mship/core/worktree.py` — `spawn` method:**

The setup invocation currently looks like:

```python
actual_setup = repo_config.tasks.get("setup", "setup")
self._shell.run_task(
    task_name="setup",
    actual_task_name=actual_setup,
    cwd=wt_path,
    env_runner=repo_config.env_runner or self._config.env_runner,
)
```

Change `WorktreeManager.spawn` to accept a `skip_setup: bool = False` parameter and to collect setup failures:

```python
def spawn(
    self,
    description: str,
    repos: list[str] | None = None,
    skip_setup: bool = False,
) -> SpawnResult:
    """Returns a SpawnResult with the task and any setup warnings."""
    # ... existing spawn logic ...

    setup_warnings: list[str] = []

    # In the per-repo loop, replace the setup block with:
    if not skip_setup:
        actual_setup = repo_config.tasks.get("setup", "setup")
        setup_result = self._shell.run_task(
            task_name="setup",
            actual_task_name=actual_setup,
            cwd=effective,  # or wt_path for non-git_root repos
            env_runner=repo_config.env_runner or self._config.env_runner,
        )
        if setup_result.returncode != 0:
            setup_warnings.append(
                f"{repo_name}: setup failed (task '{actual_setup}') — "
                f"{setup_result.stderr.strip()[:200]}"
            )

    # ... at the end, return SpawnResult(task=task, setup_warnings=setup_warnings)
```

Add a `SpawnResult` dataclass:

```python
from dataclasses import dataclass, field

@dataclass
class SpawnResult:
    task: Task
    setup_warnings: list[str] = field(default_factory=list)
```

Update the return type and the log entry to note when setup was skipped.

**`src/mship/cli/worktree.py` — `spawn` command:**

Add `--skip-setup` flag and handle the new return type:

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
        output.json({
            **task.model_dump(mode="json"),
            "setup_warnings": result.setup_warnings,
        })
```

### Visibility During Setup

Printing a single "Running setup..." line is the minimum. For long-running setups (pnpm install can take 30s+), users still see nothing until it finishes. That's acceptable for v1 — the message at least signals that something is happening.

Future improvement: stream setup output in real time. Out of scope for this change.

### Backward Compatibility

- `WorktreeManager.spawn` now returns `SpawnResult` instead of `Task`. The only caller in the codebase is the CLI, which is updated simultaneously. Tests need to be updated to use `result.task`.
- `skip_setup` defaults to `False`, preserving current behavior when not specified.
- The setup task is still invoked on `git_root` subdirectory services using the subdirectory path (existing behavior).

## Files Changed

| File | Change | Purpose |
|------|--------|---------|
| `src/mship/util/shell.py` | Modify | `run_streaming` uses process group flags |
| `src/mship/cli/exec.py` | Modify | `_kill_group` helper; signal the group, not just the PID |
| `src/mship/core/worktree.py` | Modify | `spawn` accepts `skip_setup`, returns `SpawnResult` with warnings |
| `src/mship/cli/worktree.py` | Modify | `spawn` CLI adds `--skip-setup`, prints "Running setup...", shows warnings |
| `tests/util/test_shell.py` | Modify | Test process group flags are set |
| `tests/cli/test_exec.py` | Modify | Test kill_group is called instead of terminate |
| `tests/core/test_worktree.py` | Modify | Update to use `result.task`; test setup_warnings on failure; test `skip_setup=True` skips setup |
| `tests/cli/test_worktree.py` | Modify | Test `--skip-setup` flag |
