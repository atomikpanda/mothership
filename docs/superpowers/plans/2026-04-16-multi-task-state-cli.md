# Multi-task state and CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `WorkspaceState.current_task`, resolve every task-scoped CLI command via cwd → `MSHIP_TASK` env → `--task` flag, flock-protect state mutations, and update the pre-commit hook to allow commits in any registered worktree.

**Architecture:** Infrastructure-first (flock, resolver, CLI helper) → migrate command-by-command so each commit leaves the test suite green → finally delete the `current_task` field and sweep test fixtures. The resolver is strict: no implicit "only one active" fallback. The pre-commit hook becomes worktree-aware rather than current-task-aware.

**Tech Stack:** Python 3.12, pydantic v2, typer, pytest + pytest-asyncio, Typer `CliRunner`, stdlib `fcntl`. Spec: `docs/superpowers/specs/2026-04-16-multi-task-state-cli-design.md`.

---

## Working directory

All work happens in the pre-existing task worktree:
`/home/bailey/development/repos/mothership/.worktrees/feat/multi-task-state-and-cli-support-n-parallel-active-tasks-cwdenvflag-anchoring-flock-concurrency`

Branch: `feat/multi-task-state-and-cli-support-n-parallel-active-tasks-cwdenvflag-anchoring-flock-concurrency`.

Before any edit or commit: `cd` into the worktree. Verify with `git branch --show-current`; if you see `main`, stop immediately. Tests run via `uv run pytest ...`. The post-commit hook emits a benign `No such command '_log-commit'` — ignore.

---

## File structure

**New files:**
- `src/mship/core/task_resolver.py` — `resolve_task()` + exception classes.
- `src/mship/cli/_resolve.py` — `resolve_or_exit()` CLI glue.
- `tests/core/test_task_resolver.py` — unit tests.
- `tests/core/test_state_lock.py` — flock concurrency tests.
- `tests/cli/test_multi_task.py` — end-to-end multi-task integration.

**Modified files (migrations):**
- `src/mship/core/state.py` — add `_locked`, `mutate`, remove `current_task`.
- `src/mship/cli/status.py` — bimodal output.
- `src/mship/cli/phase.py`, `block.py`, `log.py`, `switch.py` — resolve_or_exit.
- `src/mship/cli/view/status.py`, `journal.py`/`logs.py`, `diff.py`, `spec.py` — resolve_or_exit where needed.
- `src/mship/cli/exec.py` — `test`, `run`, `logs`.
- `src/mship/cli/worktree.py` — `spawn`, `finish`, `close`, listing subcommands.
- `src/mship/cli/prune.py` + `src/mship/core/prune.py` — protect all registered worktrees.
- `src/mship/cli/internal.py` — `_check-commit`, `_post-checkout`, `_journal-commit`.
- `src/mship/core/switch.py`, `src/mship/core/worktree.py` — accept Task/slug params.

**Test sweep (Task 13):** ~89 call sites across 23 files using `state.current_task` as a setter/getter.

---

## Migration recipe (context for Tasks 4–12)

Every task-scoped CLI command follows this pattern. It's spelled out per-task below, but here's the underlying template:

```python
# BEFORE
def some_command(...):
    container = get_container()
    output = Output()
    state = container.state_manager().load()
    if state.current_task is None:
        output.error("No active task...")
        raise typer.Exit(code=1)
    task = state.tasks[state.current_task]
    # ... use task.slug, task.phase, etc.
```

```python
# AFTER
def some_command(
    ...,
    task: Optional[str] = typer.Option(None, "--task", help="Target task (default: cwd/env)"),
):
    from mship.cli._resolve import resolve_or_exit
    container = get_container()
    output = Output()
    state = container.state_manager().load()
    t = resolve_or_exit(state, task)
    # ... use t.slug, t.phase, etc. — `t` replaces `task` from before
```

Note: if the surrounding code uses the name `task`, rename the local to `t` so the CLI option can keep the user-facing `--task` name.

State mutations go through `state_mgr.mutate(lambda s: ...)` instead of `load()` → modify → `save()`. The lambda receives the state inside the lock; any mutations persist atomically.

---

## Task 1: Flock primitive + `StateManager.mutate`

**Files:**
- Modify: `src/mship/core/state.py`
- Test: `tests/core/test_state_lock.py` (new)

- [ ] **Step 1: Write failing concurrency test**

Create `tests/core/test_state_lock.py`:

```python
import multiprocessing
from pathlib import Path

import pytest

from mship.core.state import StateManager, WorkspaceState, Task
from datetime import datetime, timezone


def _append_task(state_dir_str: str, slug: str):
    """Subprocess body: open its own StateManager and mutate to add a task."""
    state_dir = Path(state_dir_str)
    sm = StateManager(state_dir)

    def _mutate(s: WorkspaceState):
        s.tasks[slug] = Task(
            slug=slug,
            description=f"from {slug}",
            phase="plan",
            created_at=datetime(2026, 4, 16, tzinfo=timezone.utc),
            affected_repos=[],
            branch=f"feat/{slug}",
        )
    sm.mutate(_mutate)


def test_mutate_serializes_concurrent_writers(tmp_path: Path):
    sm = StateManager(tmp_path)
    sm.save(WorkspaceState())

    procs = [
        multiprocessing.Process(target=_append_task, args=(str(tmp_path), f"t{i}"))
        for i in range(5)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    state = sm.load()
    assert set(state.tasks.keys()) == {"t0", "t1", "t2", "t3", "t4"}


def test_shared_read_does_not_block_readers(tmp_path: Path):
    """Two concurrent load() calls should both return quickly — shared locks don't block each other."""
    import threading
    import time

    sm = StateManager(tmp_path)
    sm.save(WorkspaceState(tasks={"x": Task(
        slug="x", description="d", phase="plan",
        created_at=datetime(2026, 4, 16, tzinfo=timezone.utc),
        affected_repos=[], branch="feat/x",
    )}))

    results = []

    def _reader():
        t0 = time.monotonic()
        for _ in range(50):
            sm.load()
        results.append(time.monotonic() - t0)

    threads = [threading.Thread(target=_reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # If shared locks blocked each other, total time scales with thread count.
    # Assert each reader finished in under 2s — generous headroom for CI.
    assert all(r < 2.0 for r in results), f"readers took too long: {results}"
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/multi-task-state-and-cli-support-n-parallel-active-tasks-cwdenvflag-anchoring-flock-concurrency
uv run pytest tests/core/test_state_lock.py -v
```

Expected: `AttributeError: 'StateManager' object has no attribute 'mutate'` on the first test; the second test may also fail for the same reason once `load()` tries to use the new lock path.

- [ ] **Step 3: Add `_locked` + refactor `load`/`save` + add `mutate`**

Edit `src/mship/core/state.py`. At the top of the file (after existing imports), add:

```python
import fcntl
from contextlib import contextmanager
from typing import Callable
```

Then add this module-level helper above `StateManager`:

```python
@contextmanager
def _locked(state_dir: Path, mode: int):
    """Advisory lock on `<state_dir>/state.lock`.

    mode: fcntl.LOCK_SH (shared read) or fcntl.LOCK_EX (exclusive write).
    Released when the context exits.
    """
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

Rename the existing `load` body to `_load_nolock` and `save` body to `_save_nolock`; wrap them with locked versions. Replace the full `StateManager` class with:

```python
class StateManager:
    """Read/write .mothership/state.yaml with atomic writes + flock."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._state_file = state_dir / "state.yaml"

    def _load_nolock(self) -> WorkspaceState:
        if not self._state_file.exists():
            return WorkspaceState()
        with open(self._state_file) as f:
            raw = yaml.safe_load(f)
        if raw is None:
            return WorkspaceState()
        return WorkspaceState(**raw)

    def _save_nolock(self, state: WorkspaceState) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        data = state.model_dump(mode="json")
        for task in data.get("tasks", {}).values():
            task["worktrees"] = {
                k: str(v) for k, v in task.get("worktrees", {}).items()
            }
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

    def load(self) -> WorkspaceState:
        with _locked(self._state_dir, fcntl.LOCK_SH):
            return self._load_nolock()

    def save(self, state: WorkspaceState) -> None:
        with _locked(self._state_dir, fcntl.LOCK_EX):
            self._save_nolock(state)

    def mutate(self, fn: "Callable[[WorkspaceState], None]") -> WorkspaceState:
        """Read-modify-write under one exclusive lock. No lost updates."""
        with _locked(self._state_dir, fcntl.LOCK_EX):
            state = self._load_nolock()
            fn(state)
            self._save_nolock(state)
            return state

    def get_current_task(self) -> Task | None:
        """Legacy accessor — returns None when there's no singular current task.
        Kept for transitional compatibility; callers should migrate to resolve_task."""
        state = self.load()
        if state.current_task is None:
            return None
        return state.tasks.get(state.current_task)
```

Leave `WorkspaceState.current_task` as-is for now (Task 13 removes it).

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/core/test_state_lock.py -v
```

Expected: both tests pass.

- [ ] **Step 5: Run the full suite for regressions**

```bash
uv run pytest -q
```

Expected: green. `load()` and `save()` now take a flock but are behaviorally identical to callers.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/state.py tests/core/test_state_lock.py
git commit -m "feat(state): advisory flock + StateManager.mutate

Wrap load/save in fcntl.flock (shared for reads, exclusive for writes).
Add StateManager.mutate(fn) for read-modify-write under one exclusive
lock. Prepares multi-task CLI where concurrent agents on different
tasks all mutate the same state.yaml."
```

---

## Task 2: Task resolver module

**Files:**
- Create: `src/mship/core/task_resolver.py`
- Test: `tests/core/test_task_resolver.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/core/test_task_resolver.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.state import Task, WorkspaceState
from mship.core.task_resolver import (
    resolve_task,
    NoActiveTaskError,
    UnknownTaskError,
    AmbiguousTaskError,
)


def _task(slug: str, worktrees: dict[str, Path]) -> Task:
    return Task(
        slug=slug,
        description=f"desc for {slug}",
        phase="plan",
        created_at=datetime(2026, 4, 16, tzinfo=timezone.utc),
        affected_repos=list(worktrees.keys()),
        branch=f"feat/{slug}",
        worktrees={k: Path(v) for k, v in worktrees.items()},
    )


def test_cli_task_match(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    state = WorkspaceState(tasks={"A": _task("A", {"r": wt})})
    t = resolve_task(state, cli_task="A", env_task=None, cwd=tmp_path)
    assert t.slug == "A"


def test_cli_task_miss_raises(tmp_path: Path):
    state = WorkspaceState(tasks={"A": _task("A", {})})
    with pytest.raises(UnknownTaskError) as exc:
        resolve_task(state, cli_task="B", env_task=None, cwd=tmp_path)
    assert exc.value.slug == "B"


def test_env_task_match(tmp_path: Path):
    state = WorkspaceState(tasks={"A": _task("A", {})})
    t = resolve_task(state, cli_task=None, env_task="A", cwd=tmp_path)
    assert t.slug == "A"


def test_env_task_miss_raises(tmp_path: Path):
    state = WorkspaceState(tasks={"A": _task("A", {})})
    with pytest.raises(UnknownTaskError):
        resolve_task(state, cli_task=None, env_task="C", cwd=tmp_path)


def test_cwd_inside_worktree_resolves(tmp_path: Path):
    wt = tmp_path / "A_wt"; wt.mkdir()
    state = WorkspaceState(tasks={"A": _task("A", {"r": wt})})
    t = resolve_task(state, cli_task=None, env_task=None, cwd=wt)
    assert t.slug == "A"


def test_cwd_deep_inside_worktree_resolves(tmp_path: Path):
    wt = tmp_path / "A_wt"; wt.mkdir()
    deep = wt / "src" / "foo"; deep.mkdir(parents=True)
    state = WorkspaceState(tasks={"A": _task("A", {"r": wt})})
    t = resolve_task(state, cli_task=None, env_task=None, cwd=deep)
    assert t.slug == "A"


def test_zero_tasks_raises_no_active(tmp_path: Path):
    state = WorkspaceState(tasks={})
    with pytest.raises(NoActiveTaskError):
        resolve_task(state, cli_task=None, env_task=None, cwd=tmp_path)


def test_two_tasks_no_anchor_raises_ambiguous(tmp_path: Path):
    state = WorkspaceState(tasks={
        "A": _task("A", {}),
        "B": _task("B", {}),
    })
    with pytest.raises(AmbiguousTaskError) as exc:
        resolve_task(state, cli_task=None, env_task=None, cwd=tmp_path)
    assert exc.value.active == ["A", "B"]


def test_flag_beats_env_beats_cwd(tmp_path: Path):
    wtA = tmp_path / "A_wt"; wtA.mkdir()
    state = WorkspaceState(tasks={
        "A": _task("A", {"r": wtA}),
        "B": _task("B", {}),
        "C": _task("C", {}),
    })
    # cwd inside A_wt, env="B", flag="C" -> flag wins
    assert resolve_task(state, cli_task="C", env_task="B", cwd=wtA).slug == "C"
    # env="B", flag=None, cwd inside A_wt -> env wins
    assert resolve_task(state, cli_task=None, env_task="B", cwd=wtA).slug == "B"
    # env=None, flag=None, cwd inside A_wt -> cwd wins
    assert resolve_task(state, cli_task=None, env_task=None, cwd=wtA).slug == "A"
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/core/test_task_resolver.py -v
```

Expected: every test fails with `ModuleNotFoundError: No module named 'mship.core.task_resolver'`.

- [ ] **Step 3: Implement the resolver**

Create `src/mship/core/task_resolver.py`:

```python
"""Resolve which task a CLI invocation targets.

Priority: --task flag > MSHIP_TASK env > cwd → worktree → task.
Strict: no implicit fallback to "the only active task" — if nothing
resolves, raise AmbiguousTaskError (2+ active) or NoActiveTaskError (0).
"""
from __future__ import annotations

from pathlib import Path

from mship.core.state import Task, WorkspaceState


class NoActiveTaskError(Exception):
    """No tasks exist in workspace state."""


class UnknownTaskError(Exception):
    """A named task (flag or env) doesn't exist in workspace state."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"Unknown task: {slug}")
        self.slug = slug


class AmbiguousTaskError(Exception):
    """Multiple active tasks and no anchor (cwd/env/flag) could disambiguate."""

    def __init__(self, active: list[str]) -> None:
        super().__init__(f"Multiple active tasks: {', '.join(active)}")
        self.active = active


def resolve_task(
    state: WorkspaceState,
    *,
    cli_task: str | None,
    env_task: str | None,
    cwd: Path,
) -> Task:
    # 1. Explicit --task flag wins.
    if cli_task is not None:
        if cli_task in state.tasks:
            return state.tasks[cli_task]
        raise UnknownTaskError(cli_task)

    # 2. MSHIP_TASK env var.
    if env_task:
        if env_task in state.tasks:
            return state.tasks[env_task]
        raise UnknownTaskError(env_task)

    # 3. Walk cwd upward — first match wins.
    cwd_resolved = cwd.resolve()
    for task in state.tasks.values():
        for wt_path in task.worktrees.values():
            wt_resolved = Path(wt_path).resolve()
            try:
                cwd_resolved.relative_to(wt_resolved)
                return task
            except ValueError:
                continue

    # 4. No anchor resolved.
    if not state.tasks:
        raise NoActiveTaskError(
            "no active task; run `mship spawn \"description\"` to start one"
        )
    raise AmbiguousTaskError(sorted(state.tasks.keys()))
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/core/test_task_resolver.py -v
```

Expected: all 9 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/task_resolver.py tests/core/test_task_resolver.py
git commit -m "feat(core): task resolver (cwd → env → flag)

resolve_task() walks the resolution chain and raises typed exceptions
on miss/ambiguous/empty. Strict behavior — no implicit 'only one
active task' fallback. Used by every task-scoped CLI command in the
multi-task model."
```

---

## Task 3: CLI `resolve_or_exit` helper

**Files:**
- Create: `src/mship/cli/_resolve.py`
- Test: `tests/cli/test_resolve_helper.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/cli/test_resolve_helper.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer

from mship.core.state import Task, WorkspaceState
from mship.cli._resolve import resolve_or_exit


def _task(slug: str) -> Task:
    return Task(
        slug=slug, description="d", phase="plan",
        created_at=datetime(2026, 4, 16, tzinfo=timezone.utc),
        affected_repos=[], branch=f"feat/{slug}",
    )


def test_returns_task_when_resolved(monkeypatch, tmp_path):
    state = WorkspaceState(tasks={"A": _task("A")})
    monkeypatch.setenv("MSHIP_TASK", "A")
    monkeypatch.chdir(tmp_path)
    t = resolve_or_exit(state, cli_task=None)
    assert t.slug == "A"


def test_no_active_exits_nonzero(monkeypatch, tmp_path, capsys):
    state = WorkspaceState(tasks={})
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit) as exc:
        resolve_or_exit(state, cli_task=None)
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "no active task" in err.lower()


def test_unknown_task_exits_nonzero_lists_known(monkeypatch, tmp_path, capsys):
    state = WorkspaceState(tasks={"A": _task("A")})
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit):
        resolve_or_exit(state, cli_task="nope")
    err = capsys.readouterr().err
    assert "nope" in err
    assert "A" in err


def test_ambiguous_exits_nonzero_lists_active(monkeypatch, tmp_path, capsys):
    state = WorkspaceState(tasks={"A": _task("A"), "B": _task("B")})
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.Exit):
        resolve_or_exit(state, cli_task=None)
    err = capsys.readouterr().err
    assert "A" in err and "B" in err
    assert "--task" in err or "MSHIP_TASK" in err
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/cli/test_resolve_helper.py -v
```

Expected: `ModuleNotFoundError: No module named 'mship.cli._resolve'`.

- [ ] **Step 3: Implement**

Create `src/mship/cli/_resolve.py`:

```python
"""CLI glue for mship.core.task_resolver.

Catches the three resolver exceptions, writes a friendly error to stderr,
and raises typer.Exit(1). Usage:

    t = resolve_or_exit(state, cli_task)  # `cli_task` comes from --task option
"""
from __future__ import annotations

import os
from pathlib import Path

import typer

from mship.cli.output import Output
from mship.core.state import Task, WorkspaceState
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
    resolve_task,
)


def resolve_or_exit(state: WorkspaceState, cli_task: str | None) -> Task:
    output = Output()
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

If `Output.error` doesn't exist in `cli/output.py`, check the existing module and use whichever write-to-stderr helper is established (`output.print(..., err=True)` or similar). Adjust the three exception branches accordingly. Keep the semantics (message to stderr + `typer.Exit(1)`).

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/cli/test_resolve_helper.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/_resolve.py tests/cli/test_resolve_helper.py
git commit -m "feat(cli): resolve_or_exit helper

Wraps resolve_task with friendly CLI error output and typer.Exit(1).
Single entrypoint for every task-scoped command's target resolution."
```

---

## Task 4: Migrate `mship spawn` (drop `current_task` write)

**Files:**
- Modify: `src/mship/cli/worktree.py` (the `spawn` function, ~line 66)
- Modify: `src/mship/core/worktree.py` (line 180 sets `state.current_task = slug`)

- [ ] **Step 1: Identify the current_task writes in spawn's path**

Read `src/mship/cli/worktree.py:spawn` and `src/mship/core/worktree.py:180`. The core function sets `state.current_task = slug`. That line is the only behavioral write we need to remove in spawn's path.

- [ ] **Step 2: Remove the `current_task` assignment**

Edit `src/mship/core/worktree.py`. Find the line:

```python
        state.current_task = slug
```

(around line 180, inside the spawn-helper function). Delete that line. The surrounding `state.tasks[slug] = new_task` (or equivalent — confirm name by reading) must remain.

If the surrounding code uses `state.current_task` immediately afterward (e.g., for the return value or log), also update those reads to use the just-added task directly.

- [ ] **Step 3: Ensure spawn still uses `state_mgr.mutate` for atomicity**

If spawn currently does `state = load(); ... ; save(state)`, wrap it in `state_mgr.mutate(lambda s: ...)` instead. Concretely, inside `src/mship/core/worktree.py:spawn` (whichever function builds the task), the surrounding block becomes:

```python
def _spawn_mutation(state: WorkspaceState) -> None:
    # existing body that built `new_task` and assigned into state.tasks
    state.tasks[slug] = new_task
    # DO NOT set state.current_task

state_mgr.mutate(_spawn_mutation)
```

Adapt variable names to what the current function uses. If any state needs to flow out (e.g., the final `new_task` for display), capture it in a nonlocal before return.

- [ ] **Step 4: Run spawn-adjacent tests**

```bash
uv run pytest tests/cli/test_worktree.py tests/core/test_worktree.py -v
```

Expected: spawn creates a task; some existing tests may assert `state.current_task == slug` after spawn. Those assertions now fail — that's expected. **Do not fix them in this task.** They'll be swept in Task 13.

For now, mark expected-broken tests with an inline xfail or pytest.skip("superseded by multi-task — see Task 13") so CI stays green through the intermediate commits. Specifically, look for `assert state.current_task` patterns in `tests/cli/test_worktree.py` and `tests/core/test_worktree.py` and add:

```python
    pytest.skip("obsolete — current_task removed in multi-task migration")
```

at the top of each offending test function, OR add `@pytest.mark.xfail(reason="multi-task migration — Task 13 sweeps tests")`.

Prefer `pytest.skip` at the first line: safer than xfail when the test would otherwise produce false positives.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -q
```

Expected: green, possibly with extra `SKIPPED` lines for obsolete assertions.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/worktree.py tests/cli/test_worktree.py tests/core/test_worktree.py
git commit -m "feat(spawn): drop current_task write

Spawn no longer mutates state.current_task. The task is still
persisted into state.tasks[slug]; subsequent commands resolve it
via cwd/env/flag. Tests that asserted the old behavior are
explicitly skipped with a reference to the multi-task migration."
```

---

## Task 5: Migrate `mship status` to bimodal output

**Files:**
- Modify: `src/mship/cli/status.py`
- Test: `tests/cli/test_status.py` (update)

- [ ] **Step 1: Write failing test for workspace summary mode**

Append to `tests/cli/test_status.py`:

```python
from datetime import datetime, timezone
from typer.testing import CliRunner
from mship.cli import app, container
from mship.core.state import StateManager, WorkspaceState, Task


def _mk_workspace(tmp_path, tasks: dict[str, str]):
    """Create a workspace with the given {slug: phase} map."""
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state = WorkspaceState(tasks={
        slug: Task(
            slug=slug, description="d", phase=phase,
            created_at=datetime(2026, 4, 16, tzinfo=timezone.utc),
            affected_repos=[], branch=f"feat/{slug}",
        )
        for slug, phase in tasks.items()
    })
    StateManager(state_dir).save(state)

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    return state_dir, cfg


def _reset_container():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def test_status_no_tasks_emits_empty_active_list(tmp_path, monkeypatch):
    _mk_workspace(tmp_path, {})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    try:
        import json
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        assert data == {"active_tasks": []}
    finally:
        _reset_container()


def test_status_multiple_tasks_no_anchor_lists_all(tmp_path, monkeypatch):
    _mk_workspace(tmp_path, {"A": "dev", "B": "review"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    try:
        import json
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        slugs = [t["slug"] for t in data["active_tasks"]]
        assert set(slugs) == {"A", "B"}
        # Each entry has slug/phase/branch keys at minimum
        for t in data["active_tasks"]:
            assert "slug" in t and "phase" in t and "branch" in t
    finally:
        _reset_container()


def test_status_resolves_via_task_flag(tmp_path, monkeypatch):
    _mk_workspace(tmp_path, {"A": "dev", "B": "review"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)
    try:
        import json
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(app, ["status", "--task", "A"])
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["slug"] == "A"
    finally:
        _reset_container()
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/cli/test_status.py -v
```

Expected: the three new tests fail; `status` doesn't yet emit `active_tasks` and doesn't accept `--task`.

- [ ] **Step 3: Rewrite `mship status`**

Replace `src/mship/cli/status.py:status` (the full function, lines ~8-109) with:

```python
    @app.command()
    def status(
        task: Optional[str] = typer.Option(
            None, "--task", help="Target task (default: cwd/env)"
        ),
    ):
        """Show status of a task (resolved from cwd/env/flag) or workspace summary."""
        from datetime import datetime, timezone
        from mship.util.duration import format_relative
        from mship.cli._resolve import resolve_or_exit
        from mship.core.task_resolver import (
            AmbiguousTaskError, NoActiveTaskError, resolve_task,
        )
        import os
        from pathlib import Path

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        # Decide: workspace summary or single-task detail.
        # We try resolve_task ourselves so we can route both "no active" and
        # "ambiguous (no anchor)" to the summary, but unknown --task / unknown
        # MSHIP_TASK remain errors.
        t = None
        if task is not None or os.environ.get("MSHIP_TASK"):
            # Explicit target — resolve_or_exit shows a friendly error on miss.
            t = resolve_or_exit(state, task)
        else:
            try:
                t = resolve_task(
                    state, cli_task=None, env_task=None, cwd=Path.cwd(),
                )
            except (NoActiveTaskError, AmbiguousTaskError):
                t = None

        if t is None:
            # Workspace summary.
            active = sorted(
                state.tasks.values(),
                key=lambda tt: (tt.phase_entered_at or tt.created_at),
                reverse=True,
            )
            if output.is_tty:
                if not active:
                    output.print("No active tasks. Run `mship spawn \"description\"`.")
                else:
                    output.print(f"[bold]Active tasks ({len(active)}):[/bold]")
                    for tt in active:
                        phase_rel = (
                            format_relative(tt.phase_entered_at)
                            if tt.phase_entered_at else "—"
                        )
                        output.print(
                            f"  {tt.slug}  "
                            f"phase={tt.phase} (entered {phase_rel})  "
                            f"branch={tt.branch}"
                        )
            else:
                output.json({
                    "active_tasks": [
                        {
                            "slug": tt.slug,
                            "phase": tt.phase,
                            "branch": tt.branch,
                            "phase_entered_at": (
                                tt.phase_entered_at.isoformat()
                                if tt.phase_entered_at else None
                            ),
                        }
                        for tt in active
                    ],
                })
            return

        # Single-task detail — reuse the existing body, using `t` instead of
        # `state.tasks[state.current_task]`.
        task_obj = t

        # Drift (local-only)
        drift_summary: dict = {"has_errors": False, "error_count": 0}
        try:
            from mship.core.repo_state import audit_repos
            from mship.core.audit_gate import collect_known_worktree_paths
            config = container.config()
            shell = container.shell()
            try:
                known = collect_known_worktree_paths(state_mgr)
            except Exception:
                known = frozenset()
            report = audit_repos(
                config, shell, names=task_obj.affected_repos,
                known_worktree_paths=known, local_only=True,
            )
            errors = [i for r in report.repos for i in r.issues if i.severity == "error"]
            drift_summary = {"has_errors": bool(errors), "error_count": len(errors)}
        except Exception:
            pass

        last_log: dict | None = None
        try:
            entries = container.log_manager().read(task_obj.slug, last=1)
            if entries:
                e = entries[-1]
                first_line = e.message.splitlines()[0] if e.message else ""
                last_log = {"message": first_line[:60], "timestamp": e.timestamp}
        except Exception:
            last_log = None

        if output.is_tty:
            output.print(f"[bold]Task:[/bold] {task_obj.slug}")
            if task_obj.finished_at is not None:
                output.print(
                    f"[yellow]⚠ Finished:[/yellow] {format_relative(task_obj.finished_at)} — run `mship close` after merge"
                )
            if task_obj.active_repo is not None:
                output.print(f"[bold]Active repo:[/bold] {task_obj.active_repo}")
            phase_str = task_obj.phase
            if task_obj.phase_entered_at is not None:
                rel = format_relative(task_obj.phase_entered_at)
                phase_str = f"{task_obj.phase} (entered {rel})"
            if task_obj.blocked_reason:
                phase_str = f"{phase_str}  [red]BLOCKED:[/red] {task_obj.blocked_reason}"
            output.print(f"[bold]Phase:[/bold] {phase_str}")
            if task_obj.blocked_at:
                output.print(f"[bold]Blocked since:[/bold] {task_obj.blocked_at}")
            output.print(f"[bold]Branch:[/bold] {task_obj.branch}")
            output.print(f"[bold]Repos:[/bold] {', '.join(task_obj.affected_repos)}")
            if task_obj.worktrees:
                output.print("[bold]Worktrees:[/bold]")
                for repo, path in task_obj.worktrees.items():
                    output.print(f"  {repo}: {path}")
            if task_obj.test_results:
                output.print("[bold]Tests:[/bold]")
                for repo, result in task_obj.test_results.items():
                    status_str = (
                        "[green]pass[/green]" if result.status == "pass"
                        else "[red]fail[/red]"
                    )
                    output.print(f"  {repo}: {status_str}")
            if drift_summary["has_errors"]:
                output.print(
                    f"[bold]Drift:[/bold] [red]{drift_summary['error_count']} error(s)[/red] — run `mship audit`"
                )
            else:
                output.print("[bold]Drift:[/bold] [green]clean[/green]")
            if last_log is not None:
                ts_rel = format_relative(last_log["timestamp"])
                output.print(f"[bold]Last log:[/bold] \"{last_log['message']}\" ({ts_rel})")
        else:
            data = task_obj.model_dump(mode="json")
            data["active_repo"] = task_obj.active_repo
            if task_obj.blocked_reason:
                data["phase_display"] = f"{task_obj.phase} (BLOCKED: {task_obj.blocked_reason})"
            if task_obj.finished_at is not None:
                data["close_hint"] = "mship close"
            data["drift"] = drift_summary
            data["last_log"] = (
                {"message": last_log["message"], "timestamp": last_log["timestamp"].isoformat()}
                if last_log is not None else None
            )
            output.json(data)
```

Add the missing import at the top of `src/mship/cli/status.py`:

```python
from typing import Optional
import typer
```

(Most likely `typer` is already imported.)

- [ ] **Step 4: Run status tests**

```bash
uv run pytest tests/cli/test_status.py -v
```

Expected: new tests pass. Existing tests may fail — update them to set cwd or pass `--task`. Preserve assertions; update the setup only.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -q
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/status.py tests/cli/test_status.py
git commit -m "feat(status): bimodal — workspace summary or single-task detail

Resolve via cwd/env/flag. When nothing resolves and 0/2+ tasks are
active, emit a workspace summary listing all active tasks (JSON:
active_tasks: []). When resolved, emit today's single-task detail
output shape unchanged."
```

---

## Task 6: Migrate `phase`, `block`, `unblock`

**Files:**
- Modify: `src/mship/cli/phase.py`
- Modify: `src/mship/cli/block.py`
- Test: `tests/cli/test_phase.py`, `tests/cli/test_block.py` (update setup — chdir or `--task`)

- [ ] **Step 1: Migrate `phase.py`**

Edit `src/mship/cli/phase.py`. Replace the command body with the migration recipe pattern. Using the current structure as the starting point:

```python
    @app.command()
    def phase(
        target: str = typer.Argument(...),
        force: bool = typer.Option(False, "-f", "--force"),
        task: Optional[str] = typer.Option(
            None, "--task", help="Target task (default: cwd/env)"
        ),
    ):
        """Transition the task's phase."""
        from mship.cli._resolve import resolve_or_exit
        container = get_container()
        state_mgr = container.state_manager()
        state = state_mgr.load()
        t = resolve_or_exit(state, task)

        # existing logic uses t.slug / t.phase / etc. instead of state.current_task
        # Wrap the transition in state_mgr.mutate to persist atomically.

        def _apply(s):
            task_ref = s.tasks[t.slug]
            # ... existing transition code, using task_ref in place of
            # state.tasks[state.current_task]
            ...

        state_mgr.mutate(_apply)
        ...  # existing output logic, using t
```

Preserve every existing behavior: `--force` flag for unblock+transition, warnings on soft-gate failures (no spec / tests-not-run / uncommitted), blocked-task refusal. The only change is the task lookup and the wrapping of the mutation in `mutate()`.

Add at the top of the file:
```python
from typing import Optional
```
if absent.

- [ ] **Step 2: Migrate `block.py`**

Edit `src/mship/cli/block.py`. Both `block` and `unblock` follow the same recipe. Current `block` (lines ~17-32):

```python
        state = container.state_manager().load()
        if state.current_task is None:
            output.error("No active task...")
            raise typer.Exit(code=1)
        task = state.tasks[state.current_task]
        ...
        log_mgr.append(state.current_task, f"Blocked: {reason}")
```

Rewrite as:

```python
        from mship.cli._resolve import resolve_or_exit
        container = get_container()
        state_mgr = container.state_manager()
        state = state_mgr.load()
        t = resolve_or_exit(state, task_opt)
        log_mgr = container.log_manager()

        def _apply(s):
            s.tasks[t.slug].blocked_reason = reason
            s.tasks[t.slug].blocked_at = datetime.now(timezone.utc)
        state_mgr.mutate(_apply)
        log_mgr.append(t.slug, f"Blocked: {reason}")
        output.print(...)
        if not output.is_tty:
            output.json({"task": t.slug, "blocked_reason": reason})
```

Add a `task_opt: Optional[str] = typer.Option(None, "--task", help="Target task (default: cwd/env)")` parameter. Same treatment for `unblock`: resolve, mutate to clear `blocked_reason`/`blocked_at`, log, emit.

Preserve existing guards (e.g., `block` refuses if already blocked, `unblock` refuses if not blocked).

- [ ] **Step 3: Update tests**

Both test files (`tests/cli/test_phase.py`, `tests/cli/test_block.py`) currently rely on `state.current_task` being set. For each test:

- Change setup to either `monkeypatch.chdir(task.worktrees["mothership"])` (if the test creates a worktree) or append `"--task", slug` to the CliRunner args.

Preserve the assertion intent — only change the invocation to supply an anchor.

- [ ] **Step 4: Run target tests**

```bash
uv run pytest tests/cli/test_phase.py tests/cli/test_block.py -v
```

Expected: green.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -q
```

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/phase.py src/mship/cli/block.py tests/cli/test_phase.py tests/cli/test_block.py
git commit -m "feat(cli): migrate phase/block/unblock to task resolver

Replace state.current_task reads with resolve_or_exit(state, task);
wrap state mutations in state_mgr.mutate. Tests updated to pass
--task or chdir into the worktree."
```

---

## Task 7: Migrate `switch` and `journal` (CLI)

**Files:**
- Modify: `src/mship/cli/switch.py`
- Modify: `src/mship/cli/log.py` (hosts `mship journal`)
- Test: update corresponding test files

- [ ] **Step 1: Migrate `switch.py`**

Apply the migration recipe. Current structure (lines ~27-31):

```python
        state = container.state_manager().load()
        if state.current_task is None:
            output.error("...")
            raise typer.Exit(1)
        task = state.tasks[state.current_task]
```

Rewrite:

```python
        from mship.cli._resolve import resolve_or_exit
        container = get_container()
        state_mgr = container.state_manager()
        state = state_mgr.load()
        t = resolve_or_exit(state, task_opt)

        def _apply(s):
            s.tasks[t.slug].active_repo = repo
            # preserve the SHA snapshot logic that already exists here —
            # it reads from t.last_switched_at_sha etc.
            ...
        state_mgr.mutate(_apply)
```

Add `task_opt: Optional[str] = typer.Option(None, "--task", ...)`.

Note: `switch`'s semantics don't change — it still operates within a single task; `--task` just picks which one.

- [ ] **Step 2: Migrate `log.py`**

The `mship journal` command in `src/mship/cli/log.py` has two modes: write (`mship journal "msg"` with optional flags) and read (`mship journal --last N`). Both read `state.current_task`.

For the write path, resolve via the helper, then `log_mgr.append(t.slug, ...)`. For the read path, resolve then `log_mgr.read(t.slug, last=...)`.

Add `--task` to both. Preserve `--all`, `--action`, `--open`, `--test-state`, `--repo`, `--iteration`, `--last`, and `--show-open` options unchanged in behavior.

- [ ] **Step 3: Update tests**

`tests/cli/test_log.py` and any `tests/cli/test_switch.py`-equivalent: update invocations to pass `--task` or chdir.

- [ ] **Step 4: Run target tests + full suite**

```bash
uv run pytest tests/cli/test_switch.py tests/cli/test_log.py -v
uv run pytest -q
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/switch.py src/mship/cli/log.py tests/cli/test_switch.py tests/cli/test_log.py
git commit -m "feat(cli): migrate switch + journal to task resolver"
```

(If `tests/cli/test_switch.py` doesn't exist, omit from the add.)

---

## Task 8: Migrate view commands (`status`, `journal`, `diff`, `spec`)

**Files:**
- Modify: `src/mship/cli/view/logs.py` (journal), `status.py`, `diff.py`, `spec.py`
- Test: `tests/cli/view/test_logs_view.py`, `test_status_view.py`, `test_diff_view.py`, `test_spec_view.py`

- [ ] **Step 1: Audit which view commands need `--task` added**

`view diff` already has `--task` (verified: `src/mship/cli/view/diff.py` line ~331). `view spec` has `--task` (line ~152). `view journal` has `--task` (`logs.py` line ~56). Check `view status` — likely missing.

For each view, the resolution logic today is roughly:

```python
task_slug = task if task is not None else state.current_task
if task_slug is None:
    # ... fallback handling, often error
```

Migrate to:

```python
from mship.cli._resolve import resolve_or_exit
t = resolve_or_exit(state, task)
task_slug = t.slug
```

Crucially, views also have a `--watch` flag, so the resolution happens at invocation time (startup), not per frame.

- [ ] **Step 2: Migrate each view's `register` function**

For each of the four views (`view/status.py`, `view/logs.py`, `view/diff.py`, `view/spec.py`):

1. Ensure `task: Optional[str] = typer.Option(None, "--task", ...)` is present.
2. Replace the `task_slug = task if task is not None else state.current_task` pattern with `t = resolve_or_exit(state, task); task_slug = t.slug`.
3. Preserve the picker-fallback path: when `task is None` (no flag, but possibly cwd/env anchor), the picker is no longer needed for the "multiple active" case — the resolver will either return a task or emit an Ambiguous error. The picker still makes sense when no task is active at all, but since `NoActiveTaskError` fires, there's no picker path to hit. **Keep the picker code for the "no active task" branch only if the current UX requires a picker**; otherwise remove it.

  Simplest approach: delete the picker code and let `resolve_or_exit` handle all the "no task resolvable" cases. Document in the commit.

  Audit: the picker appears at `src/mship/cli/view/diff.py:375` and `src/mship/cli/view/spec.py:190` and `src/mship/cli/view/logs.py:91`. If removal feels risky, leave it but guarded behind `not state.tasks` (i.e., "no tasks at all"). The picker with zero tasks → user picks nothing → quiet exit, fine.

- [ ] **Step 3: Update tests**

The view test files currently set `state.current_task` directly. Change each test to: spawn a task, then either `monkeypatch.chdir(worktree)` or pass `--task slug` on the CLI invocation.

- [ ] **Step 4: Run target tests + full suite**

```bash
uv run pytest tests/cli/view/ -v
uv run pytest -q
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/view/ tests/cli/view/
git commit -m "feat(view): migrate status/journal/diff/spec to task resolver

All four views resolve target via resolve_or_exit(state, --task).
Removed the cross-task picker path in favor of clear Ambiguous errors
when no anchor is provided."
```

---

## Task 9: Migrate exec commands (`test`, `run`, `logs`)

**Files:**
- Modify: `src/mship/cli/exec.py`
- Test: `tests/cli/test_exec.py`

- [ ] **Step 1: Migrate each subcommand**

`src/mship/cli/exec.py` has three functions: `test_cmd`, `run_cmd`, `logs`. Each reads `state.current_task` early (lines 58, 211, 334).

Apply the migration recipe to each. Concretely, each function gains `task: Optional[str] = typer.Option(None, "--task", help="Target task (default: cwd/env)")` and replaces the `if state.current_task is None: output.error(...)` block with `t = resolve_or_exit(state, task)`. Every downstream use of `state.current_task` becomes `t.slug` (and `state.tasks[state.current_task]` becomes `t`).

Preserve `--repos`, `--tag`, `--all`, `--no-diff`, and any other flags unchanged.

For the mutation where `test` appends an iteration record to state, wrap in `state_mgr.mutate`:

```python
def _record(s):
    s.tasks[t.slug].test_iteration += 1
    s.tasks[t.slug].test_results = {...}
state_mgr.mutate(_record)
```

- [ ] **Step 2: Update tests**

`tests/cli/test_exec.py`: pass `--task` or chdir before invoking the CLI. Preserve assertions.

- [ ] **Step 3: Run target tests + full suite**

```bash
uv run pytest tests/cli/test_exec.py tests/core/test_executor.py -v
uv run pytest -q
```

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add src/mship/cli/exec.py tests/cli/test_exec.py tests/core/test_executor.py
git commit -m "feat(exec): migrate test/run/logs to task resolver"
```

---

## Task 10: Migrate `finish`, `close`, and worktree-listing subcommands

**Files:**
- Modify: `src/mship/cli/worktree.py` (hosts `spawn`, `finish`, `close`, listing)
- Test: `tests/cli/test_worktree.py`, `tests/test_finish_integration.py`

- [ ] **Step 1: Migrate `finish` and `close`**

In `src/mship/cli/worktree.py` (spawn was already handled in Task 4). `finish` (~line 343) and `close` (~line 196) both read `state.current_task`.

Apply the recipe to both:
- Add `task: Optional[str] = typer.Option(None, "--task", ...)`.
- Call `t = resolve_or_exit(state, task)`.
- Use `t.slug` / `t` everywhere `state.current_task` / `state.tasks[state.current_task]` appeared.
- Wrap state mutations in `state_mgr.mutate`.

`close` deletes the task from `state.tasks`; that deletion goes inside the mutate lambda:

```python
def _close(s):
    del s.tasks[t.slug]
state_mgr.mutate(_close)
```

`finish` updates `finished_at`, writes PR URLs, etc. Same pattern.

- [ ] **Step 2: Migrate listing subcommands in `worktree.py`**

Lines ~183 and ~212 currently reference `state.current_task`. Look at those functions — they likely iterate all tasks with a " (active)" marker for the current one. Rewrite: show all active tasks; no special marker needed (every task in `tasks` is active).

```python
for slug, task in sorted(state.tasks.items()):
    # remove `active = " (active)" if slug == state.current_task else ""`
    output.print(f"{slug}: {task.worktrees}")
```

For the JSON path, replace `{"current_task": state.current_task, "tasks": data}` with `{"tasks": data}`.

- [ ] **Step 3: Update tests**

`tests/cli/test_worktree.py` has 19 `current_task` references. For each test: either spawn via the CLI (which no longer sets current_task), chdir into the worktree, pass `--task`, or update the assertion to read from `state.tasks[slug]` directly.

`tests/test_finish_integration.py` (1 reference): same pattern.

- [ ] **Step 4: Run target tests + full suite**

```bash
uv run pytest tests/cli/test_worktree.py tests/test_finish_integration.py -v
uv run pytest -q
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_worktree.py tests/test_finish_integration.py
git commit -m "feat(cli): migrate finish/close/worktree-listing to task resolver

Removed ' (active)' markers from listing output — every task in
state.tasks is active in the multi-task model. Close removes the
task from state atomically inside state_mgr.mutate."
```

---

## Task 11: Migrate `prune` (CLI + core)

**Files:**
- Modify: `src/mship/cli/prune.py`
- Modify: `src/mship/core/prune.py` (lines 96-97)
- Test: `tests/core/test_prune.py`, `tests/cli/test_prune.py`

- [ ] **Step 1: Migrate `core/prune.py`**

Current logic at `src/mship/core/prune.py:96-97` clears `state.current_task = None` when the pruned task matched it. With the field gone, this is a no-op. Delete those two lines (they're inside a conditional):

```python
                                if state.current_task == task_slug:
                                    state.current_task = None
```

Additionally, prune today might protect the active-task worktree via a similar `current_task` check. Scan the file for any worktree-protection logic and change it to: **"don't prune any worktree that appears in any active task's `worktrees` dict."**

```python
protected_paths = {
    Path(wt).resolve()
    for task in state.tasks.values()
    for wt in task.worktrees.values()
}
# skip any worktree whose path is in protected_paths
```

- [ ] **Step 2: Migrate `cli/prune.py`**

If the CLI reads `state.current_task`, update to use only `state.tasks` (there's no "current" task to special-case anymore). Prune is workspace-scoped — no `--task` flag needed.

- [ ] **Step 3: Update tests**

`tests/core/test_prune.py` has 4 references; `tests/cli/test_prune.py` may have some. Replace `state.current_task = "X"` setups with `state.tasks["X"] = Task(...)`; replace assertions on `state.current_task` post-prune with assertions on `state.tasks`.

- [ ] **Step 4: Run target tests + full suite**

```bash
uv run pytest tests/core/test_prune.py tests/cli/test_prune.py -v
uv run pytest -q
```

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/prune.py src/mship/cli/prune.py tests/core/test_prune.py tests/cli/test_prune.py
git commit -m "feat(prune): protect every registered worktree

Prune no longer special-cases the current task; it protects every
worktree across all active tasks in state.tasks."
```

---

## Task 12: Migrate internal hooks (`_check-commit`, `_post-checkout`, `_journal-commit`)

**Files:**
- Modify: `src/mship/cli/internal.py`
- Test: `tests/cli/test_check_commit.py`, `tests/cli/test_internal_hooks.py`, `tests/test_hook_integration.py`

- [ ] **Step 1: Rewrite `_check-commit`**

The spec's Section 4 shows the full target logic. Replace the body of `check_commit` in `src/mship/cli/internal.py` (lines 8-82) with:

```python
    @app.command(name="_check-commit", hidden=True)
    def check_commit(toplevel: str = typer.Argument(..., help="git rev-parse --show-toplevel value")):
        """Exit 0 if committing at `toplevel` is allowed under the active tasks.

        Rules:
        - No active tasks -> allow (exit 0).
        - Active tasks but toplevel not in any registered worktree -> reject (exit 1).
        - toplevel matches an active task's worktree -> allow (after reconcile gate).

        Fail-open on any exception (corrupt state, missing config, etc.) -> exit 0.
        """
        try:
            container = get_container()
            state = container.state_manager().load()
        except Exception:
            raise typer.Exit(code=0)

        if not state.tasks:
            raise typer.Exit(code=0)

        try:
            tl = Path(toplevel).resolve()
            registered = [
                (slug, Path(wt).resolve())
                for slug, task in state.tasks.items()
                for wt in task.worktrees.values()
            ]
        except (OSError, RuntimeError):
            raise typer.Exit(code=0)

        matched_task = None
        for slug, wt in registered:
            if tl == wt:
                matched_task = state.tasks[slug]
                break

        if matched_task is not None:
            # Reconcile gate (per-task, unchanged behavior)
            try:
                from mship.core.reconcile.cache import ReconcileCache
                from mship.core.reconcile.fetch import (
                    collect_git_snapshots, fetch_pr_snapshots,
                )
                from mship.core.reconcile.gate import (
                    GateAction, reconcile_now, should_block,
                )
                cache = ReconcileCache(container.state_dir())

                def _fetcher(branches, worktrees_by_branch):
                    return (
                        fetch_pr_snapshots(branches),
                        collect_git_snapshots(worktrees_by_branch),
                    )

                decisions = reconcile_now(state, cache=cache, fetcher=_fetcher)
            except Exception:
                raise typer.Exit(code=0)

            ignored = cache.read_ignores()
            d = decisions.get(matched_task.slug)
            if d is not None:
                action = should_block(d, command="precommit", ignored=ignored)
                if action is GateAction.block:
                    import sys
                    sys.stderr.write(
                        f"\u26d4 mship: refusing commit — task '{matched_task.slug}' has "
                        f"{d.state.value} drift"
                        + (f" (PR #{d.pr_number}).\n" if d.pr_number else ".\n")
                        + "   Run `mship reconcile` for details, or `git commit --no-verify` to override.\n"
                    )
                    raise typer.Exit(code=1)
            raise typer.Exit(code=0)

        # No match — reject with list of active worktrees.
        import sys
        sys.stderr.write(
            f"\u26d4 mship: refusing commit — {tl} is not a registered worktree.\n"
            f"   Active task worktrees:\n"
        )
        for slug, wt in registered:
            sys.stderr.write(f"     {wt} ({slug})\n")
        sys.stderr.write(
            f"   cd into one of those, or use `git commit --no-verify` to override.\n"
        )
        raise typer.Exit(code=1)
```

- [ ] **Step 2: Rewrite `_post-checkout`**

Replace the body of `post_checkout` in `src/mship/cli/internal.py` (lines 85-153) with a version that uses cwd-based task resolution (swallowing resolver errors for non-essential post-checkout warnings):

```python
    @app.command(name="_post-checkout", hidden=True)
    def post_checkout(
        prev_head: str = typer.Argument(...),
        new_head: str = typer.Argument(...),
    ):
        """Warn loudly when the checkout doesn't match any active task's worktree."""
        import subprocess
        import sys
        from pathlib import Path

        try:
            container = get_container()
            state = container.state_manager().load()
        except Exception:
            raise typer.Exit(code=0)

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, cwd=Path.cwd(),
            )
        except Exception:
            raise typer.Exit(code=0)
        if result.returncode != 0:
            raise typer.Exit(code=0)
        current_branch = result.stdout.strip()

        if current_branch in {"main", "master", "develop"}:
            raise typer.Exit(code=0)

        if not state.tasks:
            sys.stderr.write(
                f"\u26a0 mship: checked out '{current_branch}' but no active mship task.\n"
                f"  If you're starting feature work, run `mship spawn \"<description>\"`.\n"
            )
            raise typer.Exit(code=0)

        cwd = Path.cwd().resolve()
        matched_task = None
        for task in state.tasks.values():
            for wt in task.worktrees.values():
                try:
                    cwd.relative_to(Path(wt).resolve())
                    matched_task = task
                    break
                except ValueError:
                    continue
            if matched_task is not None:
                break

        if matched_task is None:
            active = ", ".join(sorted(state.tasks.keys()))
            sys.stderr.write(
                f"\u26a0 mship: you checked out '{current_branch}' outside any active worktree.\n"
                f"  Active tasks: {active}\n"
                f"  cd into one of the registered worktrees before editing.\n"
            )
            raise typer.Exit(code=0)

        if current_branch != matched_task.branch:
            sys.stderr.write(
                f"\u26a0 mship: checked out '{current_branch}' but the matched worktree\n"
                f"  belongs to task '{matched_task.slug}' on '{matched_task.branch}'.\n"
            )
        raise typer.Exit(code=0)
```

- [ ] **Step 3: Rewrite `_journal-commit`**

Replace the body of `journal_commit` (lines 155-216) with cwd-based resolution:

```python
    @app.command(name="_journal-commit", hidden=True)
    def journal_commit():
        """Auto-append a commit record to the task whose worktree contains cwd."""
        import subprocess
        from pathlib import Path

        try:
            container = get_container()
            state = container.state_manager().load()
        except Exception:
            raise typer.Exit(code=0)

        if not state.tasks:
            raise typer.Exit(code=0)

        cwd = Path.cwd().resolve()
        matched_task = None
        matched_repo: str | None = None
        for task in state.tasks.values():
            for repo_name, wt_path in task.worktrees.items():
                wt_resolved = Path(wt_path).resolve()
                try:
                    cwd.relative_to(wt_resolved)
                    matched_task = task
                    matched_repo = repo_name
                    break
                except ValueError:
                    continue
            if matched_task is not None:
                break

        if matched_task is None:
            raise typer.Exit(code=0)

        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%H%n%s"],
                cwd=cwd, capture_output=True, text=True, check=False,
            )
        except Exception:
            raise typer.Exit(code=0)
        if result.returncode != 0:
            raise typer.Exit(code=0)

        lines = result.stdout.splitlines()
        if not lines:
            raise typer.Exit(code=0)
        sha = lines[0].strip()
        subject = lines[1].strip() if len(lines) > 1 else ""

        try:
            container.log_manager().append(
                matched_task.slug,
                f"commit {sha[:10]}: {subject}",
                repo=matched_repo,
                iteration=matched_task.test_iteration if matched_task.test_iteration else None,
                action="committed",
            )
        except Exception:
            pass
        raise typer.Exit(code=0)
```

- [ ] **Step 4: Extend hook integration test**

Append to `tests/test_hook_integration.py`:

```python
def test_check_commit_allows_any_registered_worktree(tmp_path, monkeypatch):
    """Two active tasks; commit in either worktree is allowed."""
    # spawn A and B, each gets a worktree; commit in A's worktree → exit 0
    # commit in B's worktree → exit 0
    # commit in main checkout → exit 1
    ...  # use the existing test scaffolding in this file for setup


def test_check_commit_rejects_main_checkout_when_tasks_active(tmp_path, monkeypatch):
    ...


def test_check_commit_allows_any_cwd_when_no_tasks(tmp_path, monkeypatch):
    ...
```

(Flesh out using the existing test patterns in that file — read it first to see how workspace/spawn setup is already done there.)

- [ ] **Step 5: Update existing hook tests**

`tests/cli/test_check_commit.py` (1 ref) and `tests/cli/test_internal_hooks.py` (5 refs): update to the new model. Any test that set `state.current_task = X` directly should instead add the task via `state.tasks[X] = Task(...)` with worktrees populated.

- [ ] **Step 6: Run target tests + full suite**

```bash
uv run pytest tests/cli/test_check_commit.py tests/cli/test_internal_hooks.py tests/test_hook_integration.py -v
uv run pytest -q
```

Expected: green.

- [ ] **Step 7: Commit**

```bash
git add src/mship/cli/internal.py tests/cli/test_check_commit.py tests/cli/test_internal_hooks.py tests/test_hook_integration.py
git commit -m "feat(hooks): multi-task-aware pre-commit, post-checkout, journal-commit

Pre-commit accepts commits in any registered worktree (was: only the
current task's). Post-checkout and journal-commit infer the target
task from cwd. Commits outside all worktrees while tasks are active
are still rejected."
```

---

## Task 13: Remove `WorkspaceState.current_task` + test sweep

**Files:**
- Modify: `src/mship/core/state.py`
- Modify: `src/mship/core/switch.py` (lines 179-180 still read current_task)
- Modify: `src/mship/core/worktree.py` (lines 221-222 still reference current_task)
- Modify: **all tests and helpers that still set or read `state.current_task`**
- Remove: the `pytest.skip` markers added in Task 4

- [ ] **Step 1: Remove lingering `current_task` references in `core/`**

Grep for remaining reads:

```bash
grep -rn "current_task" src/mship/ --include="*.py"
```

You'll see `src/mship/core/state.py` (the definition), `src/mship/core/switch.py:179-180`, and `src/mship/core/worktree.py:221-222`. For each:

- `core/switch.py:179-180`: these lines assert `state.current_task is not None` inside a core function. The CLI now passes `t` down; refactor the function signature to accept a `task: Task` parameter and use it directly. CLI caller in `cli/switch.py` already resolved `t` — pass it in.

- `core/worktree.py:221-222`: inside the close/finish flow. Remove the `if state.current_task == task_slug: state.current_task = None` block entirely — there's no `current_task` field to clear.

- [ ] **Step 2: Remove the field from `WorkspaceState`**

Edit `src/mship/core/state.py`. Change `WorkspaceState` to:

```python
from pydantic import BaseModel, ConfigDict

class WorkspaceState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tasks: dict[str, Task] = {}
```

Remove `current_task: str | None = None` and the `get_current_task()` method. The `extra="ignore"` directive silently drops any `current_task:` key still present in on-disk `state.yaml`.

- [ ] **Step 3: Sweep tests**

Grep the tests directory:

```bash
grep -rn "current_task" tests/ --include="*.py" | wc -l
```

Expect ~89 hits across 23 files. For each:

- `state.current_task = X` setter: delete the line OR replace with a proper `state.tasks[X] = Task(...)` if the surrounding test needs the task to exist.
- `state.current_task == X` assertion: replace with `X in state.tasks` or delete if the test was only verifying the pointer.
- `state.current_task is None` assertion: replace with `not state.tasks` or update to check ambiguity / resolution.

Also remove the `pytest.skip("obsolete...")` markers added in Task 4 — those tests are now genuinely obsolete; either repair their assertions or delete the test method if superseded by `tests/cli/test_multi_task.py` in Task 14.

Work file-by-file; after each file run its tests to catch mistakes early:

```bash
uv run pytest tests/<file>.py -v
```

Files by priority (highest first, based on count):
- `tests/cli/test_worktree.py` (19 refs)
- `tests/core/test_state.py` (15 refs)
- `tests/cli/view/test_status_view.py` (9 refs)
- `tests/cli/test_exec.py` (7 refs)
- `tests/cli/test_internal_hooks.py` (5 refs)
- `tests/cli/view/test_spec_view.py` (4 refs)
- `tests/core/test_prune.py` (4 refs)
- `tests/cli/test_cli_help_improvements.py` (3 refs)
- `tests/core/test_executor.py` (3 refs)
- `tests/core/test_worktree.py` (3 refs)
- `tests/cli/test_status.py` (2 refs — should mostly be gone post-Task-5)
- `tests/cli/test_reconcile.py` (2 refs)
- `tests/cli/view/test_logs_view.py` (2 refs)
- `tests/core/test_phase.py` (2 refs)
- Plus singletons in `test_finish_integration.py`, `conftest.py`, `test_monorepo_integration.py`, `test_block.py`, `test_phase.py`, `test_check_commit.py`, `test_log.py`, `test_audit.py`, `test_diff_view.py`.

- [ ] **Step 4: Run full suite**

```bash
uv run pytest -q
```

Expected: green.

- [ ] **Step 5: Verify no production code reads `current_task`**

```bash
grep -rn "current_task" src/mship/ --include="*.py"
```

Expected: empty output (or only references in comments / docstrings — those are fine).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(state): remove WorkspaceState.current_task

Complete the multi-task migration: delete the singular current_task
field from WorkspaceState and sweep every test fixture that set or
asserted on it. state.yaml with the old field still loads cleanly
(extra='ignore'). Breaks 'mship status | jq .current_task' — the
no-task path now emits {active_tasks: [...]} instead."
```

---

## Task 14: End-to-end integration tests

**Files:**
- Create: `tests/cli/test_multi_task.py`

- [ ] **Step 1: Write the integration test file**

Create `tests/cli/test_multi_task.py`:

```python
"""End-to-end multi-task scenarios exercising cwd/env/flag anchoring."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, WorkspaceState, Task
from datetime import datetime, timezone


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Minimal mship workspace with no tasks; tests spawn their own."""
    cfg = tmp_path / "mothership.yaml"
    # Minimal config: one repo 'r' pointing to a tmp git root.
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"],
                   cwd=repo_dir, check=True)
    cfg.write_text(
        "workspace: t\n"
        f"repos:\n  r:\n    path: {repo_dir}\n    type: service\n"
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    StateManager(state_dir).save(WorkspaceState())

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)

    yield tmp_path

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def _spawn(runner, desc):
    result = runner.invoke(app, ["spawn", desc, "--skip-setup"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_two_tasks_coexist(workspace):
    runner = CliRunner(mix_stderr=False)
    a = _spawn(runner, "first task")
    b = _spawn(runner, "second task")
    assert a["slug"] != b["slug"]
    state = StateManager(workspace / ".mothership").load()
    assert set(state.tasks.keys()) == {a["slug"], b["slug"]}


def test_status_no_anchor_lists_both_tasks(workspace, monkeypatch):
    runner = CliRunner(mix_stderr=False)
    a = _spawn(runner, "first")
    b = _spawn(runner, "second")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert {t["slug"] for t in data["active_tasks"]} == {a["slug"], b["slug"]}


def test_phase_without_anchor_errors_ambiguous(workspace):
    runner = CliRunner(mix_stderr=False)
    _spawn(runner, "first")
    _spawn(runner, "second")
    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code == 1
    assert "multiple active" in result.stderr.lower() or "--task" in result.stderr


def test_phase_with_task_flag_transitions_correct_task(workspace):
    runner = CliRunner(mix_stderr=False)
    a = _spawn(runner, "first")
    b = _spawn(runner, "second")
    result = runner.invoke(app, ["phase", "dev", "--task", a["slug"]])
    assert result.exit_code == 0, result.stderr
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks[a["slug"]].phase == "dev"
    assert state.tasks[b["slug"]].phase == "plan"


def test_env_anchor_scopes_session(workspace, monkeypatch):
    runner = CliRunner(mix_stderr=False)
    a = _spawn(runner, "first")
    _spawn(runner, "second")
    monkeypatch.setenv("MSHIP_TASK", a["slug"])
    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code == 0, result.stderr
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks[a["slug"]].phase == "dev"


def test_cwd_inside_worktree_resolves(workspace, monkeypatch):
    runner = CliRunner(mix_stderr=False)
    a = _spawn(runner, "first")
    _spawn(runner, "second")
    a_wt = Path(a["worktrees"]["r"])
    monkeypatch.chdir(a_wt)
    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code == 0, result.stderr
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks[a["slug"]].phase == "dev"


def test_journal_writes_to_resolved_task(workspace, monkeypatch):
    runner = CliRunner(mix_stderr=False)
    a = _spawn(runner, "first")
    b = _spawn(runner, "second")
    monkeypatch.chdir(Path(b["worktrees"]["r"]))
    result = runner.invoke(app, ["journal", "hello from B"])
    assert result.exit_code == 0, result.stderr
    # A's journal should NOT contain the B message
    log_a = (workspace / ".mothership" / "logs" / f"{a['slug']}.md").read_text() \
        if (workspace / ".mothership" / "logs" / f"{a['slug']}.md").exists() else ""
    assert "hello from B" not in log_a
    # B's journal should
    log_b = (workspace / ".mothership" / "logs" / f"{b['slug']}.md").read_text()
    assert "hello from B" in log_b
```

- [ ] **Step 2: Run the integration tests**

```bash
uv run pytest tests/cli/test_multi_task.py -v
```

Expected: all pass. If any fail, the likely cause is a missed migration in an earlier task — trace and fix in the correct earlier task's commit (don't patch here).

- [ ] **Step 3: Run the full suite**

```bash
uv run pytest -q
```

Expected: green across the whole repo.

- [ ] **Step 4: Verify the `grep` for `current_task` in src/**

```bash
grep -rn "current_task" src/mship/ --include="*.py"
```

Expected: empty.

- [ ] **Step 5: Log progress + transition phase**

```bash
mship journal "Multi-task state+CLI migration complete. 14 tasks. All tests passing. Ready for review." --action "completed implementation" --test-state pass
mship phase review
```

- [ ] **Step 6: Commit**

```bash
git add tests/cli/test_multi_task.py
git commit -m "test(integration): end-to-end multi-task scenarios

Covers: two tasks coexist, status summary, ambiguous error,
--task flag, MSHIP_TASK env, cwd anchoring, per-task journal
isolation. Serves as the regression suite for the multi-task
feature."
```

---

## Self-review (performed during plan authoring)

**Spec coverage:**
- §1 state model (remove current_task, flock, mutate) → Tasks 1 + 13. ✓
- §2 task resolution → Tasks 2 + 3. ✓
- §3 CLI audit → Tasks 4–11 (one per command cluster). ✓
- §4 hooks → Task 12. ✓
- §5 spawn + journal + testing + migration → Task 4 (spawn), hook integration in 12, integration tests in 14. ✓

**Placeholder scan:** No TBD/TODO. The "confirm by reading" notes in Task 3 (about `Output.error`) and Task 8 (picker removal) are not placeholders — they're explicit instructions to verify a local assumption, with a concrete fallback if wrong.

**Type consistency:** `resolve_task` signature, `Task` field names (slug, phase, branch, worktrees, active_repo), `StateManager.mutate` signature, and `resolve_or_exit` signature are used identically across every task.

**Big-picture risk:** Task 13 (remove field + sweep tests) is the largest single commit — ~89 test changes across 23 files. Keeping it as one task preserves atomicity of the breaking change. Tasks 1-12 are each independently green-testable.
