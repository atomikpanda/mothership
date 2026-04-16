# Multi-task state and CLI

**Status:** design, approved
**Date:** 2026-04-16
**Task slug:** `multi-task-state-and-cli-support-n-parallel-active-tasks-cwdenvflag-anchoring-flock-concurrency`
**Branch:** `feat/multi-task-state-and-cli-support-n-parallel-active-tasks-cwdenvflag-anchoring-flock-concurrency`

## Summary

Teach `mship` to manage N active tasks simultaneously. Remove the singular `WorkspaceState.current_task`; every task-scoped CLI command resolves its target from cwd, `MSHIP_TASK` env var, or `--task <slug>` flag (in that priority). Protect concurrent `state.yaml` mutations with an advisory file lock. Update the pre-commit hook to accept commits in any registered worktree while rejecting commits to the main checkout when tasks are active.

This is **sub-project 1** of the "work on multiple mothership tasks in parallel + supervise from zellij" initiative. Sub-project 2 (supervision dashboard view + multi-task zellij layout) is deferred and will be specified separately after this lands.

## Motivation

The v2 product goal (per project memory) is multi-agent orchestration: one human supervising N agents, each working on a separate task in a separate worktree. The v1 single-`current_task` model forces agents and humans to serialize on a global mutable pointer; spawn a second task and you either clobber the first or invent ad-hoc per-shell bookkeeping. Neither is acceptable for v2.

The existing data model (`WorkspaceState.tasks: dict[slug, Task]`) already supports multiple tasks coexisting — only the `current_task` anchor and the commands that read it assume singularity. Removing that assumption is the minimum viable change to unblock parallel work.

Design is scoped to **state and CLI**. The supervision UX layer (dashboard views, zellij layouts for N tasks) depends on this but ships separately.

## Design

### 1. State model

Remove `current_task` from `WorkspaceState`:

```python
class WorkspaceState(BaseModel):
    tasks: dict[str, Task] = {}
```

A task is "active" iff it's in the `tasks` dict. Lifecycle is unchanged: `mship spawn` adds a task; `mship close` removes it. Finished-but-unclosed tasks stay in the dict.

**Migration:** on load, any existing `current_task` field in `state.yaml` is silently ignored. Either set `model_config = ConfigDict(extra="ignore")` on `WorkspaceState` or explicitly `raw.pop("current_task", None)` before validation. No migration script, no user action required.

**Concurrency — advisory file lock:**

A dedicated lock file `.mothership/state.lock` (separate from `state.yaml` itself, so atomic rename on save doesn't fight the lock):

```python
from contextlib import contextmanager
import fcntl

@contextmanager
def _locked(state_dir: Path, mode: int):
    """`mode` is fcntl.LOCK_SH or fcntl.LOCK_EX. Released when context exits."""
    lock_path = state_dir / "state.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as lf:
        fcntl.flock(lf, mode)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
```

`StateManager` gains a single-shot read, a single-shot save, and a preferred `mutate()` method:

```python
class StateManager:
    def load(self) -> WorkspaceState:
        with _locked(self._state_dir, fcntl.LOCK_SH):
            return self._load_nolock()

    def save(self, state: WorkspaceState) -> None:
        with _locked(self._state_dir, fcntl.LOCK_EX):
            self._save_nolock(state)

    def mutate(self, fn: Callable[[WorkspaceState], None]) -> WorkspaceState:
        """Read, mutate, write under one exclusive lock — no lost updates."""
        with _locked(self._state_dir, fcntl.LOCK_EX):
            state = self._load_nolock()
            fn(state)
            self._save_nolock(state)
            return state
```

Every CLI mutation (phase transitions, block/unblock, structured journal metadata, finish, close, switch, spawn itself) migrates from `state = load(); ...; save(state)` to `state_mgr.mutate(lambda s: ...)`. Code that only reads stays on `load()`.

### 2. Task resolution

New module `src/mship/core/task_resolver.py`:

```python
class NoActiveTaskError(Exception): ...

class UnknownTaskError(Exception):
    def __init__(self, slug: str): self.slug = slug

class AmbiguousTaskError(Exception):
    def __init__(self, active: list[str]): self.active = active


def resolve_task(
    state: WorkspaceState,
    *,
    cli_task: str | None,
    env_task: str | None,
    cwd: Path,
) -> Task:
    # 1. Explicit --task flag
    if cli_task is not None:
        if cli_task in state.tasks:
            return state.tasks[cli_task]
        raise UnknownTaskError(cli_task)

    # 2. MSHIP_TASK env var
    if env_task:
        if env_task in state.tasks:
            return state.tasks[env_task]
        raise UnknownTaskError(env_task)

    # 3. cwd upward → first matching registered worktree
    cwd_resolved = cwd.resolve()
    for task in state.tasks.values():
        for wt_path in task.worktrees.values():
            wt_resolved = Path(wt_path).resolve()
            try:
                cwd_resolved.relative_to(wt_resolved)
                return task
            except ValueError:
                continue

    # 4. Nothing resolved — strict; no implicit "only one active" fallback
    if not state.tasks:
        raise NoActiveTaskError(
            "no active task; run `mship spawn \"description\"` to start one"
        )
    raise AmbiguousTaskError(sorted(state.tasks.keys()))
```

Ordering rationale: explicit flags beat implicit inference, and cwd (the agent's natural anchor) beats env vars so an agent shelling into a worktree "just works" without needing `MSHIP_TASK`. Strict behavior when nothing resolves — no implicit fallback to a single-active task — prevents silent targeting drift as tasks come and go.

**CLI glue** at `src/mship/cli/_resolve.py`:

```python
def resolve_or_exit(state: WorkspaceState, cli_task: str | None) -> Task:
    try:
        return resolve_task(
            state,
            cli_task=cli_task,
            env_task=os.environ.get("MSHIP_TASK"),
            cwd=Path.cwd(),
        )
    except NoActiveTaskError as e:
        output.error(str(e))
        raise typer.Exit(1)
    except UnknownTaskError as e:
        known = ", ".join(sorted(state.tasks.keys())) or "(none)"
        output.error(f"Unknown task: {e.slug}. Known: {known}.")
        raise typer.Exit(1)
    except AmbiguousTaskError as e:
        output.error(
            f"Multiple active tasks ({', '.join(e.active)}). "
            "Specify --task, set MSHIP_TASK, or cd into a worktree."
        )
        raise typer.Exit(1)
```

### 3. CLI audit

#### Task-scoped commands (gain `--task` flag, call `resolve_or_exit`)

Based on current usages of `state.current_task`:

- `src/mship/cli/block.py` — `block`, `unblock`
- `src/mship/cli/phase.py` — `phase`
- `src/mship/cli/status.py` — `status` (bimodal — see below)
- `src/mship/cli/switch.py` — `switch <repo>`
- `src/mship/cli/log.py` — `mship journal` (both read and write modes; note the legacy `log` entrypoint if any still exists)
- `src/mship/cli/view/*.py` — `view status`, `view journal`, `view diff`, `view spec` (some already accept `--task`; backfill the rest and route default resolution through `resolve_or_exit`)
- `src/mship/cli/exec.py` — `test`, `run`, `logs`
- `src/mship/cli/internal.py` — `_check-commit`, `_post-checkout`, `_journal-commit`
- `src/mship/cli/worktree.py` — subcommands that show a single task's worktree(s)
- `mship finish`, `mship close` — wherever they live; likely `cli/finish.py` / `cli/close.py`

Each command adds:

```python
task: Optional[str] = typer.Option(None, "--task", help="Target task (default: cwd/env)")
```

and replaces the `if state.current_task is None: output.error(...); raise Exit(1); task = state.tasks[state.current_task]` pattern with:

```python
t = resolve_or_exit(state, task)  # task is the CLI option
# ... use t.slug everywhere state.current_task was used
```

#### Workspace-scoped commands (unchanged — no resolution)

- `mship init`, `mship doctor`, `mship sync`
- `mship graph`, `mship audit` (show all repos across the workspace by default)
- `mship layout init|launch`
- `mship spawn` (creates new; never resolves existing)

`mship prune` stays workspace-scoped but its "don't prune active-task worktrees" guard changes: today it protects the current task's worktree; now it protects **every** registered worktree across all active tasks (union of `task.worktrees.values()` for `task in state.tasks.values()`).

#### Core modules

`src/mship/core/worktree.py`, `src/mship/core/switch.py`, and `src/mship/core/prune.py` all read `state.current_task` today. Refactor: core functions take a `Task` (or `task_slug: str`) parameter; CLI callers resolve via `resolve_or_exit` and pass the result in. No core module reads `WorkspaceState.current_task` after this change — the field doesn't exist.

#### `mship status` bimodal

When called without args and no task can be resolved (0 active tasks or 2+ active with no anchor), emit a workspace summary:

```json
{
  "active_tasks": [
    {"slug": "A", "phase": "dev",    "branch": "feat/a", "last_log_at": "..."},
    {"slug": "B", "phase": "review", "branch": "feat/b", "last_log_at": "..."}
  ]
}
```

Text mode: one line per task, sorted by `phase_entered_at` desc (newest phase transitions first). Zero active tasks → `"active_tasks": []` + a friendly "no active tasks — run `mship spawn`" line in text mode.

When a task *is* resolved (via cwd/env/flag, including the user passing `--task X`), emit today's single-task detail output unchanged (minus any `current_task` key — consumers read from the top-level task fields anyway).

### 4. Hooks

#### `pre-commit` / `mship _check-commit`

Rule change: **if any tasks are active AND the commit's toplevel isn't inside a registered worktree, reject.** Otherwise allow.

```python
def check_commit(toplevel: Path, state: WorkspaceState) -> tuple[bool, str]:
    if not state.tasks:
        return True, ""  # no active tasks → no restriction

    toplevel_resolved = toplevel.resolve()
    registered: list[tuple[str, Path]] = [
        (slug, Path(wt).resolve())
        for slug, task in state.tasks.items()
        for wt in task.worktrees.values()
    ]

    for slug, wt in registered:
        if toplevel_resolved == wt:
            return True, ""

    lines = [
        f"mship: refusing commit in {toplevel} — not a registered worktree.",
        "Active task worktrees:",
    ]
    for slug, wt in registered:
        lines.append(f"  {slug}: {wt}")
    lines.append("cd into one of these worktrees, or use `git commit --no-verify` to bypass.")
    return False, "\n".join(lines)
```

No notion of "the current task" remains. The hook enforces only "commit must live in a known worktree when tasks are active." Commits outside any worktree while no tasks are active (workspace-clean state) are always allowed.

#### `post-commit` / `mship _journal-commit`

Infer task from cwd:

- Walk cwd → match against registered worktree paths → append commit-record journal entry to that task.
- If cwd doesn't match any active worktree (e.g., commit inside main checkout with no active tasks), silently no-op.

No `current_task` read.

#### `post-checkout` / `mship _post-checkout`

Current implementation: verify it doesn't reference `state.current_task`. If it does, route through `resolve_task(cli_task=None, env_task=None, cwd=Path.cwd())` and swallow `NoActiveTaskError`/`AmbiguousTaskError`/`UnknownTaskError` — post-checkout is non-essential; the fallback is to no-op.

### 5. Spawn, journal concurrency, testing, migration

#### Spawn

`mship spawn "description"`:

- Unchanged slug generation, worktree creation, symlinks, `task setup`, state persistence.
- Drops the "set current_task" step (the field is gone).
- Output JSON gains the worktree paths at the top level (already present today via `worktrees: {repo: path}`) — agents/callers can `cd` programmatically.
- Text-mode output ends with a one-line hint: `cd <first_worktree>` so a human operator knows the next step.
- Works with N tasks already active. No mutation beyond adding a new key to `state.tasks`.

#### Journal concurrency

Per-task append-only markdown files at `.mothership/logs/<slug>.md`. Different agents on different slugs never share a file, so POSIX append atomicity (`open(path, "a")`, writes ≤ PIPE_BUF ≈ 4096 bytes) covers the common case without locking. Every journal entry comfortably fits that bound.

Structured metadata that updates `task.test_iteration` et al. goes through `state.yaml`, which is flock-protected via `StateManager.mutate()` (Section 1).

Two agents racing appends on the same task's journal file is not a supported scenario (premise: one agent per task). If it happens, the file remains well-formed — POSIX atomicity guarantees that — just with unpredictable interleaving. No extra lock needed.

#### Testing

1. `tests/core/test_task_resolver.py` — unit tests:
   - `cli_task` exact match returns task.
   - `cli_task` miss raises `UnknownTaskError`.
   - `env_task` match.
   - cwd inside a registered worktree returns that task.
   - cwd deeper inside a worktree (e.g., `worktree/src/foo/`) still resolves.
   - 0 tasks + no anchor → `NoActiveTaskError`.
   - 2+ tasks + no anchor → `AmbiguousTaskError`.
   - Flag beats env beats cwd (priority order).

2. `tests/core/test_state_lock.py` — concurrency:
   - Two subprocesses each call `StateManager.mutate` to append a different task entry; final state contains both.
   - Shared read locks don't block each other (time-bound assertion).

3. `tests/cli/test_multi_task.py` — integration:
   - `mship spawn A; mship spawn B` → both in `tasks`.
   - `cd <A_worktree> && mship journal "x"` → A's journal file grew.
   - `cd <workspace_root> && mship status` → JSON contains both slugs in `active_tasks`.
   - `cd <workspace_root> && mship phase dev` → exit 1, stderr lists both slugs.
   - `mship phase dev --task B` → B transitions, A untouched.
   - `MSHIP_TASK=A mship finish` (with test double for `gh pr create`) → A finishes, B untouched.

4. `tests/test_hook_integration.py` (exists) — extend:
   - Two active tasks; commit in A's worktree → allowed.
   - Two active tasks; commit in main checkout → rejected, message lists both worktrees.
   - Zero active tasks; commit in main checkout → allowed.

5. Sweep existing tests for direct `state.current_task = "X"` setters or assertions; rewrite to use `mship spawn` + cwd/env/flag or direct `state.tasks[slug]` access. Expect ~15-25 test call sites based on the grep (`block`, `exec`, `log`, `phase`, `status`, `switch`, `worktree`, `view/*`, `internal`, `finish`, `close`).

#### Migration

- `state.yaml` with an old `current_task` field loads cleanly (field dropped on parse). No user-action migration.
- **Breaking JSON shape change:** scripts using `mship status | jq .current_task` break. The no-task path now emits `{"active_tasks": [{...}]}` instead of `{"current_task": null, "tasks": {}}`. Single-task detail output shape is unchanged. Document in the PR description, commit message, and README.
- Pre-commit hook behavior: commits in any registered worktree are now accepted (was: only the current task's worktree). Commits in main when any task is active are still rejected. Behavior strictly more permissive for in-worktree commits — no script breakage expected.
- `MSHIP_TASK` env var is new; set it per-shell when a human wants to scope a session to one task without relying on cwd.

### Out of scope (deferred to sub-project 2)

- Cross-task supervision dashboard (`mship view dashboard` or similar aggregate).
- Multi-task zellij layout (per-task tab, grid, or dynamic pane spawning).
- Agent identity / auth / HTTP bridge for external drivers.
- Task priorities, inter-task dependencies, or merge ordering across tasks.
- Per-task state file partitioning (the flock model is sufficient for expected concurrency).
- Auto-switch UX (e.g., "when I cd into a worktree, print the task info").

### Open questions

None at design-approval time.
