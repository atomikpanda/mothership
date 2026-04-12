# Monorepo Support & Service Runtime Modes Design Spec

## Overview

Three features that make mothership work for real monorepo and single-repo workspaces with mixed service types:

1. **Subdirectory services** — services that live in subdirectories of a shared git repo, not separate repos
2. **Service runtime modes** — distinguish long-running services from one-shot deploys so `mship run` doesn't block
3. **Doctor task name resolution** — fix `mship doctor` so it respects the `tasks:` mapping when checking for standard tasks

## 1. Monorepo Subdirectory Services (`git_root`)

### Problem

Today, every repo entry in `mothership.yaml` is expected to have its own `.git` directory. That breaks for monorepos where multiple services live in subdirectories of a single git repo (e.g., a FastAPI backend at repo root and a Vite frontend at `web/`).

`mship doctor` reports "not a git repository" for the subdirectory service. `mship spawn` can't create a worktree for it since there's no separate `.git`.

### Solution

Add a `git_root` field that marks a service as sharing another repo's git worktree. When set, `path` becomes relative to the git root's worktree at runtime.

### Config Syntax

```yaml
workspace: tailrd
repos:
  tailrd:
    path: .                    # absolute (resolved at load time)
    type: service
  web:
    path: web                  # relative — resolved against tailrd's worktree at runtime
    type: service
    git_root: tailrd
    depends_on: [tailrd]
```

### Path Resolution Rules

**When `git_root` is not set** (today's behavior):
- `path` resolves to an absolute directory at config load time
- `ConfigLoader.load` verifies the directory exists and has a `Taskfile.yml`

**When `git_root` is set** (new):
- `path` is stored as-is (relative string)
- Effective path at runtime: `<git_root repo's effective path> / <path>`
- For non-worktree operations: `<git_root repo's config path> / <path>`
- For worktree operations: `<git_root repo's worktree path> / <path>`
- `ConfigLoader.load` verifies `<git_root's path> / <path>` exists and has a `Taskfile.yml`

### Worktree Behavior

- `mship spawn` creates worktrees only for repos *without* `git_root` set
- For repos with `git_root`, the stored worktree path is computed as `<git_root's worktree> / <path>` and persisted to state. Example: if `tailrd`'s worktree is `/home/user/.worktrees/feat/add-feed` and `web` has `git_root: tailrd, path: web`, state stores `web`'s worktree as `/home/user/.worktrees/feat/add-feed/web`
- `mship abort` only removes worktrees for git-root owners; subdir services' paths disappear naturally when the parent worktree is removed
- `mship prune` scans only repos without `git_root` for orphan detection

### Validation Rules

- `git_root` must reference an existing repo in the same workspace
- The referenced repo must NOT itself have `git_root` set (no chaining — one level only)
- At config load, for repos with `git_root`: verify `<git_root's resolved path> / <path>` exists as a directory and contains `Taskfile.yml`
- Circular detection in cycle check still uses `depends_on` — `git_root` doesn't create dependency edges

### Graph Behavior

- Subdir services are first-class graph nodes — own `depends_on`, own `tags`, own `tasks`
- Topo sort and tier grouping treat them normally
- Only path resolution and worktree creation differ

### Model Change

Add to `RepoConfig`:

```python
git_root: str | None = None
```

No changes to `Dependency` — deps still reference repos by name regardless of `git_root`.

## 2. Service Start Mode (`start_mode`)

### Problem

`mship run` currently runs `task run` in each repo sequentially. For a monorepo with one Taskfile, this works — `task` handles parallelism and child processes internally. For multi-repo workspaces, long-running services (API, web dev server, DynamoDB local) block sequentially: the first one never exits, so the next never starts.

### Solution

Add `start_mode: foreground | background` to repos. Background services launch in threads so `mship run` can start multiple long-running services across repos.

### Config Syntax

```yaml
repos:
  shared-swift:
    path: ./shared-swift
    type: library
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
    # start_mode defaults to foreground — mship run waits for it
    depends_on: [infra]
```

### Values

- `foreground` (default) — existing behavior. `mship run` blocks until the task exits.
- `background` — `mship run` launches the task in a thread and moves on to the next repo without waiting for exit.

### Behavior in `mship run`

Within each dependency tier:

- Foreground services run via `ShellRunner.run_task()` — blocking, await exit, then continue
- Background services launch in a `ThreadPoolExecutor` thread. Each thread holds its subprocess via `subprocess.Popen`. The tier advances as soon as all background services have been *launched* (not exited — they don't)

Across tiers:

- Tier 0 completes (all foreground tasks finished, all background tasks launched) before tier 1 starts
- Tier 1 background services can launch while tier 0 background services keep running
- After the final tier, `mship run` blocks waiting for any foreground tasks, then keeps background threads alive as long as their subprocesses live

### Termination

- Ctrl-C on `mship run` propagates SIGINT to each background thread, which forwards SIGINT to its subprocess
- `task` (go-task) automatically propagates signals to its child processes, so SIGINT on `task infra:start` kills all the commands it launched
- No PID files, no explicit `mship stop` command in v1 — terminal signal handling is the termination story
- If `mship run` exits normally (all foreground finished, no background services launched), it just exits
- If there are active background services, `mship run` blocks the foreground terminal until Ctrl-C — standard dev-server UX

### Future work (noted but not in v1)

An explicit `mship stop` command with PID tracking is a natural follow-up when users hit the friction of terminal-attached supervision. Not in v1 because:

- PID tracking is fragile (PIDs get reused, state can go stale)
- Daemonization for surviving parent exit is platform-specific and complex
- Ctrl-C already works for 90% of use cases

### Impact on Other Commands

- `mship test` ignores `start_mode` — tests always run foreground (they must exit)
- `mship logs` ignores `start_mode` — `logs` always runs foreground (user expects to tail until Ctrl-C)
- `mship finish`, `mship abort`, etc. unaffected

### Model Change

Add to `RepoConfig`:

```python
start_mode: Literal["foreground", "background"] = "foreground"
```

### Implementation

`RepoExecutor.execute()` already uses `ThreadPoolExecutor` for parallel-within-tier. Extend it:

- `_execute_one` reads `repo.start_mode`
- If `foreground`: `shell.run_task()` as today (blocking, returns `ShellResult`)
- If `background`: `shell.run_streaming()` returns `Popen`, thread keeps the handle, returns a `RepoResult` with `shell_result.returncode=0` immediately (represents "launched successfully")
- A new state flag or return type distinguishes "launched and running" from "completed with exit 0" — for simplicity, mark background launches as `success=True` since they haven't failed yet
- At end of `mship run`, if any background services are active, wait on all their `Popen.wait()` calls with signal handling to propagate SIGINT

## 3. Doctor Task Name Resolution

### Problem

`mship doctor` checks each repo's Taskfile for the canonical task names (`test`, `run`, `lint`, `setup`). It does not check the `tasks:` mapping in `mothership.yaml`. So if a repo has:

```yaml
tasks:
  run: dev
```

...doctor still warns "missing task: run" even though the aliased `dev` exists.

### Solution

Doctor resolves each canonical name through the `tasks:` mapping before checking the Taskfile.

### Code Change

In `src/mship/core/doctor.py`, the loop over standard tasks:

**Before:**
```python
for task_name in ["test", "run", "lint", "setup"]:
    if task_name in task_output:
        report.checks.append(CheckResult(
            name=f"{name}/task:{task_name}", status="pass",
            message=f"task '{task_name}' available",
        ))
    else:
        report.checks.append(CheckResult(
            name=f"{name}/task:{task_name}", status="warn",
            message=f"missing task: {task_name}",
        ))
```

**After:**
```python
for canonical in ["test", "run", "lint", "setup"]:
    actual = repo.tasks.get(canonical, canonical)
    if actual in task_output:
        msg = (
            f"task '{actual}' available"
            if actual == canonical
            else f"task '{actual}' available (alias for '{canonical}')"
        )
        report.checks.append(CheckResult(
            name=f"{name}/task:{canonical}", status="pass", message=msg,
        ))
    else:
        msg = (
            f"missing task: {actual}"
            if actual == canonical
            else f"missing task: {actual} (aliased from '{canonical}')"
        )
        report.checks.append(CheckResult(
            name=f"{name}/task:{canonical}", status="warn", message=msg,
        ))
```

### Documentation Update

Add a "Task name aliasing" subsection to the README's Configuration section:

```markdown
### Task Name Aliasing

If your Taskfile uses different task names than mothership's defaults
(`test`, `run`, `lint`, `setup`), add a `tasks:` mapping:

```yaml
repos:
  my-app:
    path: .
    type: service
    tasks:
      run: dev           # mship run → task dev
      test: test:all     # mship test → task test:all
      lint: lint:all     # mship lint → task lint:all
      setup: infra:start # mship setup → task infra:start
```
```

## Files Changed/Created

| File | Change | Purpose |
|------|--------|---------|
| `src/mship/core/config.py` | Modify | Add `git_root` and `start_mode` to RepoConfig; relax path validation for `git_root` repos |
| `src/mship/core/worktree.py` | Modify | Skip worktree creation for `git_root` repos, compute their effective path |
| `src/mship/core/executor.py` | Modify | Resolve paths through `git_root`; implement `start_mode: background` via threads |
| `src/mship/core/doctor.py` | Modify | Resolve `tasks:` mapping before checking standard task names |
| `src/mship/util/shell.py` | Modify (maybe) | Ensure `run_streaming` returns a `Popen` that can be signal-handled |
| `src/mship/cli/worktree.py` | Modify | Spawn/abort handle `git_root` repos correctly |
| `src/mship/cli/exec.py` | Modify | `mship run` tracks background subprocesses, propagates SIGINT on Ctrl-C |
| `README.md` | Modify | Document `git_root`, `start_mode`, task aliasing |
| `tests/core/test_config.py` | Modify | Test `git_root` validation and `start_mode` field |
| `tests/core/test_worktree.py` | Modify | Test spawn skips `git_root` repos, path resolution |
| `tests/core/test_executor.py` | Modify | Test background launch, cwd resolution for `git_root` |
| `tests/core/test_doctor.py` | Modify | Test task name resolution through aliases |
| `tests/test_monorepo_integration.py` | Create | End-to-end test for monorepo workspace |
