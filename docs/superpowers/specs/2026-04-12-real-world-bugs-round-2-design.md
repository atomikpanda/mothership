# Real-World Bugs Round 2 & symlink_dirs Feature Design Spec

## Overview

Five bundled fixes/features discovered during dogfooding mothership on a real project:

1. **Bug: process leak on task failure** — background services' grandchildren survive when the direct child exits
2. **Bug: mship status loses active task when CWD is inside a worktree** — state dir resolved from wrong location
3. **Bug: go-task parse errors from starter Taskfile + silent doctor failures** — two related issues
4. **Feature: `symlink_dirs` per-repo config** — symlink `node_modules`, `.venv`, etc. from main repo into worktrees
5. **Minor: mship run startup summary** — show repo/task/PID on background launches

## 1. Process Leak on Child Exit

### Problem

`mship run` background mode: a task script forks `uvicorn &`, then the task fails on a later step. `Popen.wait()` returns when the *direct child* (`task dev`) exits. We report "all exited." But `uvicorn` is a grandchild that was backgrounded with `&` — it's still in the process group but still alive and holding port 8000.

Subsequent `mship run` calls fail with "Address already in use."

### Fix

After each `proc.wait()` returns, actively signal the process group to kill any surviving grandchildren.

In `cli/exec.py::run_cmd`, after the main wait loop (before the "all background services have exited" message):

```python
try:
    for proc in result.background_processes:
        proc.wait()
        # Catch any surviving grandchildren in the process group
        _kill_group(proc, signal.SIGTERM)
    # Brief grace period, then SIGKILL stragglers
    time.sleep(0.5)
    for proc in result.background_processes:
        _kill_group(proc, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
except KeyboardInterrupt:
    # existing handler unchanged
    ...
```

`_kill_group` is already tolerant of "group already gone" via `ProcessLookupError`, so calling it when everything's already exited is a no-op.

### Test

Mock `os.killpg` and verify it's called after `proc.wait()` completes successfully.

## 2. State Dir Anchored to Git Common Dir

### Problem

`ConfigLoader.discover` walks up from CWD to find `mothership.yaml`. In a worktree, `mothership.yaml` exists at the worktree root (checked out from git). State dir is computed as `config_path.parent / ".mothership"` — which becomes `<worktree>/.mothership/` when CWD is inside the worktree. That's a different location than the main repo's state.

Result: `mship status` from inside the worktree returns empty state. `mship abort --yes` fails with "No active task."

### Fix

Anchor state dir to `git rev-parse --git-common-dir`, which always resolves to the main repo's `.git/` regardless of which worktree the command runs from. State dir becomes `<main_repo>/.mothership/`.

Add a helper in `cli/__init__.py`:

```python
def _resolve_state_dir(config_path: Path) -> Path:
    """Get the workspace state dir, anchored to main repo if in a git worktree."""
    import subprocess
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
        # Not a git repo or git not installed — fall back to config path parent
        return config_path.parent / ".mothership"
```

Use it in `get_container`:

```python
if not container.state_dir.overridden:
    config_path = container.config_path()
    state_dir = _resolve_state_dir(Path(config_path))
    container.state_dir.override(state_dir)
```

### Test

Create a tmp git repo + worktree. Verify `_resolve_state_dir` returns the same path whether called with a `mothership.yaml` at the main repo root or at the worktree root.

## 3. go-task Parse Errors

### Problem 3a: Starter Taskfile breaks go-task 3.49.1

`src/mship/core/init.py::TASKFILE_TEMPLATE` currently generates:

```yaml
  test:
    cmds:
      - echo "TODO: add test command"
```

go-task 3.49.1 rejects the `"TODO:"` string with `err: invalid keys in command`. The stub setup task then fails during `mship spawn`, producing confusing warnings.

### Fix 3a

Replace all four stub `echo "TODO: ..."` lines in `TASKFILE_TEMPLATE` with `echo "TODO - ..."`. This is a one-character change (colon → dash) per line. Backward compatible with older go-task.

### Problem 3b: Doctor silent on parse failures

When `task --list` fails (returncode != 0), the doctor loop silently skips the per-task checks. Users don't see *why* their Taskfile failed — just absence of pass/warn entries.

### Fix 3b

In `core/doctor.py`, before the per-task loop, check `task --list` returncode and emit a `fail` check if non-zero:

```python
result = self._shell.run("task --list", cwd=repo.path)
if result.returncode != 0:
    err_summary = result.stderr.strip()[:200] if result.stderr else "unknown error"
    report.checks.append(CheckResult(
        name=f"{name}/taskfile_parse",
        status="fail",
        message=f"Taskfile parse error: {err_summary}",
    ))
    continue  # skip the per-task checks
# existing per-task checks
```

### Test

- New test: `DoctorChecker` emits a `fail` CheckResult when `task --list` returns non-zero
- Existing tests still pass (they mock `task --list` with returncode 0)

## 4. symlink_dirs Feature

### Problem

`node_modules`, Python `.venv`, Go module caches are large and expensive to rebuild. Today every fresh worktree requires a full reinstall. Claude Code solves this via `worktree.symlinkDirectories` — we want the same at the mothership config level.

### Config

Add `symlink_dirs: list[str] = []` to `RepoConfig`:

```yaml
repos:
  web:
    path: web
    git_root: tailrd
    symlink_dirs: [node_modules]
    tasks: {setup: setup, run: dev}
    start_mode: background
```

### Semantics

For each `dir_name` in `symlink_dirs`, create a symlink from the worktree into the source repo's matching directory. Source path:
- **Normal repo:** `repo.path / dir_name`
- **git_root repo:** `parent_repo.path / repo.path / dir_name`

Target: `<worktree_path> / dir_name`

Symlink creation happens **during `mship spawn`, after worktree creation, before `task setup`** — so setup can short-circuit when deps are unchanged.

### Conflict rules

- **Source missing** → warn `⚠ {repo}: symlink source missing: {dir_name} (will not be linked)`, continue
- **Target is a real directory** → warn `⚠ {repo}: symlink skipped, {dir_name} already exists as a real directory`, continue
- **Target is a symlink (broken or stale)** → unlink and recreate
- **Target doesn't exist** → create symlink

### Implementation

New method on `WorktreeManager`:

```python
def _create_symlinks(
    self,
    repo_name: str,
    repo_config: RepoConfig,
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

### Integration into spawn

In `WorktreeManager.spawn`, after `worktrees[repo_name] = ...` and before the `if not skip_setup:` block (both the `git_root` branch and the normal branch), call `_create_symlinks`:

```python
worktrees[repo_name] = wt_path  # or effective for git_root

# Create symlinks before setup so setup can use the linked dirs
symlink_warnings = self._create_symlinks(repo_name, repo_config, worktrees[repo_name])
setup_warnings.extend(symlink_warnings)

if not skip_setup:
    # existing setup call
    ...
```

Warnings merge into the existing `setup_warnings` list — they surface through `SpawnResult.setup_warnings` and appear in `mship spawn` output.

### Abort behavior

No change. Symlinks live inside the worktree; `git worktree remove --force` removes them. The source directory (in the main repo) is untouched.

### Test

- `_create_symlinks` creates a symlink when source exists and target doesn't
- `_create_symlinks` skips with a warning when source missing
- `_create_symlinks` skips with a warning when target is a real directory
- `_create_symlinks` replaces a stale symlink target
- End-to-end: spawn a task with `symlink_dirs: [node_modules]`, verify worktree's `node_modules` is a symlink to the source

## 5. mship run Startup Summary

### Problem

When background services start successfully, output is:
```
Started 2 background service(s). Press Ctrl-C to stop.
```

No repo names, no task names, no PIDs. Users can't confirm which services started on which ports.

### Fix

Add `background_pid: int | None = None` to `RepoResult`:

```python
@dataclass
class RepoResult:
    repo: str
    task_name: str
    shell_result: ShellResult
    skipped: bool = False
    background_pid: int | None = None
```

In `RepoExecutor._execute_one`, when launching background, set `background_pid=popen.pid` on the returned `RepoResult`:

```python
if repo_config.start_mode == "background" and canonical_task == "run":
    command = self._shell.build_command(f"task {actual_name}", env_runner)
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

Update `cli/exec.py::run_cmd` to print a summary:

```python
if not result.background_processes:
    output.success("All services started")
    return

output.success(f"Started {len(result.background_processes)} background service(s):")
for repo_result in result.results:
    if repo_result.background_pid is not None:
        output.print(f"  [green]✓[/green] {repo_result.repo} → task {repo_result.task_name}  (pid {repo_result.background_pid})")
output.print("")
output.print("Press Ctrl-C to stop.")
```

### Test

- `RepoResult.background_pid` is set when a background task is launched
- `RepoResult.background_pid` remains None for foreground tasks
- `mship run` output includes the PID line for each background service (integration test)

## Files Changed

| File | Change |
|------|--------|
| `src/mship/cli/exec.py` | Kill group after wait; startup summary with PIDs |
| `src/mship/cli/__init__.py` | `_resolve_state_dir` helper; use it in `get_container` |
| `src/mship/core/init.py` | Fix TASKFILE_TEMPLATE colons |
| `src/mship/core/doctor.py` | Fail check for `task --list` parse errors |
| `src/mship/core/config.py` | Add `symlink_dirs` to `RepoConfig` |
| `src/mship/core/worktree.py` | `_create_symlinks` method; call it in `spawn` |
| `src/mship/core/executor.py` | `RepoResult.background_pid`; set it in `_execute_one` |
| Tests for each of the above | |
