# Spawn skips setup when `task` binary missing, doctor surfaces it — Design

## Context

Every `mship spawn` in an environment without [go-task](https://taskfile.dev) installed prints:

```
mothership: setup failed (task 'setup') — /bin/sh: 1: task: not found
```

This is a once-per-workspace configuration issue — the user hasn't installed go-task, or doesn't intend to use it — but it fires on every spawn until addressed. In a session with three spawns, that's three identical warnings that never change meaning. Repeated-identical-warnings desensitize both humans and agents to real signal: a noisy floor trains readers to skim past output, and then the signal the tool actually wants to convey gets skipped too.

The issue (#51) lays out three options: (A) state-cached probe with TTL, (B) short-circuit when `shutil.which("task")` is None, (C) new config key `audit.run_setup_on_spawn`. Option B matches today's semantics best — no new surface area, no cached state to invalidate, no config opt-in. The only piece missing from B is an informational surface: `mship doctor` should report the missing binary, so the signal is visible exactly once per `doctor` invocation rather than hidden entirely.

## Goal

`mship spawn` silently skips the per-repo setup invocation when the `task` binary is not on PATH. `mship doctor` gains a `go-task` check that reports `pass` when the binary is present and `warn` when it isn't, surfacing the missing binary as actionable guidance instead of as spam on every spawn.

## Success criterion

On a machine without go-task installed:

```
$ mship spawn "tiny task"
Task spawned. Repos: …. Branch: feat/tiny-task
```

No `setup_warnings` entry related to the missing `task` binary. Output is clean.

```
$ mship doctor
…
warn   go-task            go-task not installed (https://taskfile.dev); mship will skip per-repo setup on spawn
…
```

On a machine with go-task installed, `mship spawn` runs setup as today (possibly producing legitimate per-repo setup failures, which are still warned about). `mship doctor` emits `pass   go-task   go-task found`.

## Anti-goals

- **No caching in `.mothership/state.yaml`.** Issue Option A. Introduces invalidation and TTL decisions we don't need — `shutil.which` is cheap (pure PATH scan, microseconds).
- **No new config key.** Issue Option C. Users shouldn't have to opt out of noisy default behavior; fixing the default is better.
- **No broadening to other `run_task` callers.** `mship run` / `mship test` / healthcheck task probes still surface their own "task: not found" errors when the user tries to use those commands. That's a one-time, actionable signal — not repeated noise. This design is narrowly scoped to the spawn-setup invocation.
- **No mechanism to restore the old per-spawn warning.** The signal moves to `mship doctor`. Users who disagree can reopen the issue; there is no flag to reinstate the old behavior in this change.
- **No change to what "setup" means** when the binary IS installed. Existing setup-task-fails warnings (permission errors, bad taskfile syntax, etc.) continue to surface exactly as today.

## Architecture

### `src/mship/core/worktree.py::WorktreeManager.spawn`

Two touchpoints — the `git_root` subdirectory branch and the normal-repo branch — each currently read:

```python
if not skip_setup:
    actual_setup = repo_config.tasks.get("setup", "setup")
    setup_result = self._shell.run_task(
        task_name="setup",
        actual_task_name=actual_setup,
        cwd=<wt_path>,
        env_runner=repo_config.env_runner or self._config.env_runner,
    )
    if setup_result.returncode != 0:
        setup_warnings.append(
            f"{repo_name}: setup failed (task '{actual_setup}') — "
            f"{setup_result.stderr.strip()[:200]}"
        )
```

Replace the guard in both places:

```python
if not skip_setup and shutil.which("task") is not None:
    actual_setup = repo_config.tasks.get("setup", "setup")
    setup_result = self._shell.run_task(
        task_name="setup",
        actual_task_name=actual_setup,
        cwd=<wt_path>,
        env_runner=repo_config.env_runner or self._config.env_runner,
    )
    if setup_result.returncode != 0:
        setup_warnings.append(
            f"{repo_name}: setup failed (task '{actual_setup}') — "
            f"{setup_result.stderr.strip()[:200]}"
        )
```

Add `import shutil` at the top of the module if not already imported.

`shutil.which("task")` is checked on every repo iteration rather than once-per-spawn. This is intentional and cheap: it keeps the logic local to the setup block, avoids a top-of-spawn variable that would be stale if PATH changed mid-spawn (extremely rare, but free to be correct), and matches the pattern of other one-shot probes in the codebase.

### `src/mship/core/doctor.py::Doctor.diagnose`

Add a new check near the existing `gh` check (around line 220, between `gh CLI` and `Dev-mode trap`). The check is always-on — it emits a row whether the binary is present or missing, matching the `gh` pattern:

```python
# go-task binary
if shutil.which("task") is not None:
    report.checks.append(CheckResult(
        name="go-task",
        status="pass",
        message="go-task found",
    ))
else:
    report.checks.append(CheckResult(
        name="go-task",
        status="warn",
        message=(
            "go-task not installed (https://taskfile.dev); "
            "mship will skip per-repo setup on spawn"
        ),
    ))
```

`shutil` is already imported at module top (line 1: `import os`; line 2 imports a different module — verify and add `import shutil` if missing).

The check appears in every `mship doctor` run. Workspace-level doctor runs and `mship init`'s embedded doctor pass both inherit it.

## Data flow

**`mship spawn` in an environment without `task`:**

1. CLI `spawn` → `WorktreeManager.spawn(...)`.
2. Per-repo loop: worktree created, symlinks + bind_files copied.
3. `if not skip_setup and shutil.which("task") is not None:` evaluates `shutil.which("task")` → returns `None` → guard fails → setup block skipped → no `run_task` invocation, no warning appended.
4. `SpawnResult.setup_warnings` for this repo is empty (modulo unrelated symlink/bind warnings).

**`mship spawn` with `task` installed:**

1. Same flow; `shutil.which("task")` returns a path → guard passes → existing setup path runs unchanged. A legitimate non-zero return from the user's setup task still produces the existing warning.

**`mship doctor`:**

1. Existing checks run.
2. `go-task` block: `shutil.which("task")` probes PATH; emits `pass` or `warn` row with the appropriate message.
3. Report returned with one additional row compared to today.

## Error handling

- **`shutil.which` raises** (effectively impossible — it's a pure PATH-scan over `os.environ["PATH"]`) → let it propagate. The caller's exception-handling layer is the right place to deal with an impossible condition, not this code.
- **`task` appears mid-session** (user installs it between spawns) → next `spawn` re-probes and runs setup. No stale cache.
- **`task` disappears mid-session** (PATH mutation) → next spawn silently skips setup. Doctor, if run, reports `warn`. Current behavior would have logged `task: not found` per spawn; new behavior is silent. Acceptable — the user is actively modifying their environment; they know what they did.
- **Non-existent `setup` task in a user's `Taskfile.yml`** — outside the scope of this change. `task setup` fails with a non-127 returncode; the existing warning path still fires. This change only touches the 127/task-not-on-PATH case.

## Testing

### Unit — `tests/core/test_worktree.py` (extend existing)

1. **Setup skipped when `task` missing** — use `monkeypatch.setattr("mship.core.worktree.shutil.which", lambda name: None)`. Run `spawn(...)` against a mock shell with a config that has a `setup` task defined. Assert:
   - `result.setup_warnings` contains no entry matching `"setup failed"`.
   - `mock_shell.run_task` was never called with `task_name="setup"` (tracks the skip).

2. **Setup runs normally when `task` present** — `monkeypatch.setattr("mship.core.worktree.shutil.which", lambda name: "/usr/local/bin/task")`. Same config. Run `spawn(...)`. Assert:
   - `mock_shell.run_task` was called once per repo with `task_name="setup"`.
   - When mock returns `returncode=0`, no warning.
   - When mock returns `returncode=1, stderr="broken"`, warning contains `"setup failed (task 'setup') — broken"` (verifies the existing warning path still fires for real setup failures).

3. **`skip_setup=True` bypasses the check** — regression guard. `monkeypatch.setattr("mship.core.worktree.shutil.which", lambda name: "/usr/local/bin/task")`. Run `spawn(..., skip_setup=True)`. Assert `run_task` not called. (The new guard is `and`-ed onto `not skip_setup`, so this path stays clean.)

### Unit — `tests/core/test_doctor.py` (extend existing)

1. **`go-task` pass when binary present** — `monkeypatch.setattr("mship.core.doctor.shutil.which", lambda name: "/usr/local/bin/task" if name == "task" else None)`. Run doctor. Assert the report contains a `CheckResult` with `name="go-task"`, `status="pass"`, `message="go-task found"`.

2. **`go-task` warn when binary missing** — `monkeypatch.setattr("mship.core.doctor.shutil.which", lambda name: None)`. Run doctor. Assert a `CheckResult` with `name="go-task"`, `status="warn"`, message contains `"not installed"` and `"https://taskfile.dev"`.

3. **Mixed PATH** — `shutil.which` returns a path for some binaries and None for others; the `task` check should depend only on `task`'s presence. Uses the same monkeypatch pattern with a dict lookup.

### Integration / regression

- All existing `test_worktree.py` tests continue to pass. Tests that previously expected a `"setup failed (task 'setup') — …"` warning either mock `shutil.which` to return a path (preserving the old path) or pre-install the mock behavior described above.
- All existing `test_doctor.py` tests continue to pass. The new check is additive.
- Full `pytest tests/` stays green.

### Manual smoke

In an environment without go-task installed (e.g., `PATH=/usr/bin:/bin` for a single invocation):

```bash
cd /tmp
rm -rf spawn-smoke && mkdir -p spawn-smoke && cd spawn-smoke
cat > mothership.yaml <<'EOF'
workspace: spawn-smoke
repos:
  svc:
    path: ./svc
    type: service
EOF
mkdir svc .mothership
git -C svc init -q
git -C svc commit --allow-empty -m "init"
git -C svc remote add origin /tmp/spawn-smoke/fake

PATH=/usr/bin:/bin mship spawn "test" 2>&1
```

Expect no `setup_warnings` entry about `task: not found`.

```bash
PATH=/usr/bin:/bin mship doctor 2>&1 | grep go-task
```

Expect `warn   go-task            go-task not installed …`.

Then with `task` installed (`PATH=$PATH`):

```bash
mship doctor 2>&1 | grep go-task
```

Expect `pass   go-task   go-task found`.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Scope to spawn-setup only, not all `run_task` callers | The reported pain is repeated-identical warnings on every spawn. `mship run` / `mship test` / healthcheck task probes also use `run_task`, but a user who invokes those without task installed gets a one-time actionable error — that's legitimate signal, not noise. Hiding those would mask real config problems. |
| 2 | `shutil.which` per repo, not per spawn call | Cheap probe, keeps logic local to the guard. Avoids a top-of-spawn variable that could drift if PATH changed mid-spawn (rare but free to get right). |
| 3 | Doctor row is always-on (pass + warn), not warn-only | Matches the existing `gh` check pattern. Users reading doctor output don't have to infer from silence whether a check was skipped or passed. One extra line on the happy path is cheap. |
| 4 | No new config key to restore old warning behavior | Adding a flag to opt back into noise is a worse design than fixing the default. If a user genuinely wants the warning, they can rely on `mship doctor` — which is the correct surface. |
| 5 | No caching in `.mothership/state.yaml` with TTL (issue's Option A) | `shutil.which` is microseconds; caching buys nothing measurable and adds invalidation complexity (TTL choice, reset on `mship doctor`, interaction with `uv tool install --reinstall`). |
| 6 | Message phrasing: `"go-task not installed (https://taskfile.dev); mship will skip per-repo setup on spawn"` | Names the upstream source (actionable — user can install it), and states the consequence (so the user understands why doctor is surfacing this: setup will be skipped). |
| 7 | No change to the existing "setup failed (task 'setup') — …" warning for non-127 returncodes | Real setup failures (bad Taskfile syntax, permission errors) still need to surface. The scope boundary is tight: only the "task binary not on PATH" case silences. |
