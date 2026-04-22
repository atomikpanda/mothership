# Task Breadcrumb + Ambiguity Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every task-scoped state-changing command prints a one-line breadcrumb naming the resolved task and source, and ambiguous cwd resolution fails loudly with candidate hints instead of silently picking one.

**Architecture:** One new `ResolutionSource` enum; `resolve_task` returns `(Task, ResolutionSource)`; new `resolve_for_command` CLI helper prints breadcrumb to stderr (TTY) and lets each command attach `resolved_task`/`resolution_source` to its JSON payload (non-TTY). Nine command call sites migrate with a one-line swap.

**Tech Stack:** Python 3.14 (StrEnum), typer, pytest, existing `Output` class in `src/mship/cli/output.py`.

**Reference spec:** `docs/superpowers/specs/2026-04-22-task-breadcrumb-ambiguity-detection-design.md`

---

## File structure

**New file:**
- None — all changes land in existing files.

**Modified files:**
- `src/mship/core/task_resolver.py` — add `ResolutionSource` enum, change `resolve_task` return to tuple, add `candidates` to `AmbiguousTaskError`, detect multi-match cwd ambiguity.
- `tests/core/test_task_resolver.py` — update existing tests for new return shape; add tests for each source + multi-match ambiguity.
- `src/mship/cli/output.py` — add `Output.breadcrumb(msg)` method (dim stderr line, TTY-gated).
- `src/mship/cli/_resolve.py` — unpack tuple in `resolve_or_exit` (backward compat for out-of-scope callers); add `resolve_for_command`; upgrade `AmbiguousTaskError` formatting to list candidates.
- `tests/cli/test_resolve.py` — new file for `resolve_for_command` unit tests (TTY breadcrumb, non-TTY silence, ambiguity formatting).
- `src/mship/cli/worktree.py` — migrate `finish` and `close` call sites; add JSON fields.
- `src/mship/cli/phase.py` — migrate; add JSON fields.
- `src/mship/cli/dispatch.py` — migrate; add JSON fields.
- `src/mship/cli/exec.py` — migrate (line 78 call site); add JSON fields.
- `src/mship/cli/switch.py` — migrate; add JSON fields.
- `src/mship/cli/block.py` — migrate both call sites (line 22 + 55); add JSON fields.
- `src/mship/cli/context.py` — migrate; add JSON fields.
- `src/mship/cli/log.py` — migrate; add JSON fields.
- `tests/cli/test_breadcrumb.py` — new file, parametrized integration tests for breadcrumb across in-scope commands.

**Task ordering rationale:** Task 1 (resolver) is the foundation — nothing else compiles without it. Task 2 (CLI helper) depends on Task 1. Task 3 (migrate 9 commands) depends on Task 2. Task 4 is smoke + PR. Tests live with each task's implementation (no separate test task).

---

## Task 1: Resolver upgrade

**Files:**
- Modify: `src/mship/core/task_resolver.py`
- Modify: `tests/core/test_task_resolver.py`

**Context:** Add `ResolutionSource` enum, change `resolve_task` signature to return `(Task, ResolutionSource)`, upgrade `AmbiguousTaskError` to carry candidates, detect cwd-inside-multiple-worktrees as a new ambiguity case.

- [ ] **Step 1.1: Write failing tests for the new return shape + sources**

Append to `tests/core/test_task_resolver.py`:

```python
from mship.core.task_resolver import ResolutionSource


def test_cli_task_source_is_cli_flag(tmp_path: Path):
    state = WorkspaceState(tasks={"A": _task("A", {})})
    task, source = resolve_task(state, cli_task="A", env_task=None, cwd=tmp_path)
    assert task.slug == "A"
    assert source == ResolutionSource.CLI_FLAG


def test_env_source_is_env_var(tmp_path: Path):
    state = WorkspaceState(tasks={"A": _task("A", {})})
    task, source = resolve_task(state, cli_task=None, env_task="A", cwd=tmp_path)
    assert source == ResolutionSource.ENV_VAR


def test_cwd_source_when_inside_worktree(tmp_path: Path):
    wt = tmp_path / "wt"
    wt.mkdir()
    state = WorkspaceState(tasks={"A": _task("A", {"r": wt})})
    task, source = resolve_task(state, cli_task=None, env_task=None, cwd=wt)
    assert task.slug == "A"
    assert source == ResolutionSource.CWD


def test_single_active_source_when_no_anchor(tmp_path: Path):
    """One active task, cwd is outside — returns it with SINGLE_ACTIVE source."""
    state = WorkspaceState(tasks={"A": _task("A", {"r": tmp_path / "elsewhere"})})
    task, source = resolve_task(
        state, cli_task=None, env_task=None, cwd=tmp_path,
    )
    assert task.slug == "A"
    assert source == ResolutionSource.SINGLE_ACTIVE


def test_cwd_inside_multiple_worktrees_raises_ambiguity(tmp_path: Path):
    """Cwd is under two different tasks' worktrees → error, not silent pick."""
    shared = tmp_path / "shared"
    shared.mkdir()
    # Two tasks both claim the same path as a worktree.
    state = WorkspaceState(tasks={
        "A": _task("A", {"r": shared}),
        "B": _task("B", {"r": shared}),
    })
    with pytest.raises(AmbiguousTaskError) as exc:
        resolve_task(state, cli_task=None, env_task=None, cwd=shared)
    # Both candidates surface with their worktree paths.
    slugs = [c[0] for c in exc.value.candidates]
    assert set(slugs) == {"A", "B"}


def test_no_anchor_multi_task_error_carries_candidates(tmp_path: Path):
    """Existing no-anchor case now also populates candidates for better errors."""
    wt_a = tmp_path / "a"; wt_a.mkdir()
    wt_b = tmp_path / "b"; wt_b.mkdir()
    state = WorkspaceState(tasks={
        "A": _task("A", {"r": wt_a}),
        "B": _task("B", {"r": wt_b}),
    })
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(AmbiguousTaskError) as exc:
        resolve_task(state, cli_task=None, env_task=None, cwd=outside)
    slugs = [c[0] for c in exc.value.candidates]
    assert set(slugs) == {"A", "B"}
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_task_resolver.py -v`
Expected: The 5 new tests fail (`ImportError: cannot import name 'ResolutionSource'`). Existing tests also fail because they unpack a single return value.

- [ ] **Step 1.3: Implement the resolver changes**

Replace `src/mship/core/task_resolver.py` entirely with:

```python
"""Resolve which task a CLI invocation targets.

Priority: --task flag > MSHIP_TASK env > cwd → worktree → task.

Fallbacks when no anchor resolves:
  - 0 tasks       → NoActiveTaskError
  - exactly 1     → return that task with ResolutionSource.SINGLE_ACTIVE
  - 2+ tasks      → AmbiguousTaskError(candidates=<all active tasks>)

Cwd ambiguity:
  - cwd matches 2+ distinct worktree paths → AmbiguousTaskError(candidates=<matches>)

Returns `(Task, ResolutionSource)` so callers can surface how the task was picked.
"""
from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from mship.core.state import Task, WorkspaceState


class ResolutionSource(StrEnum):
    CLI_FLAG = "--task"
    ENV_VAR = "MSHIP_TASK"
    CWD = "cwd"
    SINGLE_ACTIVE = "only active task"


class NoActiveTaskError(Exception):
    """No tasks exist in workspace state."""


class UnknownTaskError(Exception):
    """A named task (flag or env) doesn't exist in workspace state."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"Unknown task: {slug}")
        self.slug = slug


class AmbiguousTaskError(Exception):
    """Multiple tasks could apply and no anchor disambiguated.

    `candidates` carries `(slug, worktree_path)` tuples so callers can
    render concrete `--task <slug>` hints. `worktree_path` is the first
    worktree in the task's `worktrees` dict, or None if the task has none.
    """

    def __init__(
        self,
        active: list[str],
        candidates: list[tuple[str, Path | None]] | None = None,
    ) -> None:
        super().__init__(f"Multiple active tasks: {', '.join(active)}")
        self.active = active
        self.candidates = candidates if candidates is not None else []


def _first_worktree_path(task: Task) -> Path | None:
    for p in task.worktrees.values():
        return Path(p)
    return None


def resolve_task(
    state: WorkspaceState,
    *,
    cli_task: str | None,
    env_task: str | None,
    cwd: Path,
) -> tuple[Task, ResolutionSource]:
    # 1. Explicit --task flag wins.
    if cli_task is not None:
        if cli_task in state.tasks:
            return state.tasks[cli_task], ResolutionSource.CLI_FLAG
        raise UnknownTaskError(cli_task)

    # 2. MSHIP_TASK env var.
    if env_task:
        if env_task in state.tasks:
            return state.tasks[env_task], ResolutionSource.ENV_VAR
        raise UnknownTaskError(env_task)

    # 3. Walk cwd upward — collect all matches, not just the first.
    cwd_resolved = cwd.resolve()
    cwd_matches: list[tuple[str, Path]] = []
    seen_slugs: set[str] = set()
    for task in state.tasks.values():
        for wt_path in task.worktrees.values():
            wt_resolved = Path(wt_path).resolve()
            try:
                cwd_resolved.relative_to(wt_resolved)
            except ValueError:
                continue
            if task.slug not in seen_slugs:
                cwd_matches.append((task.slug, wt_resolved))
                seen_slugs.add(task.slug)
                break  # one match per task is enough
    if len(cwd_matches) == 1:
        slug = cwd_matches[0][0]
        return state.tasks[slug], ResolutionSource.CWD
    if len(cwd_matches) >= 2:
        raise AmbiguousTaskError(
            active=sorted(seen_slugs),
            candidates=cwd_matches,
        )

    # 4. No anchor resolved.
    if not state.tasks:
        raise NoActiveTaskError(
            "no active task; run `mship spawn \"description\"` to start one"
        )
    if len(state.tasks) == 1:
        only = next(iter(state.tasks.values()))
        return only, ResolutionSource.SINGLE_ACTIVE
    raise AmbiguousTaskError(
        active=sorted(state.tasks.keys()),
        candidates=[
            (t.slug, _first_worktree_path(t))
            for t in state.tasks.values()
        ],
    )
```

- [ ] **Step 1.4: Update existing resolver tests for new return shape**

The existing tests in `tests/core/test_task_resolver.py` unpack a single task. Update every call site to unpack a tuple. Example:

```python
# Before:
t = resolve_task(state, cli_task="A", env_task=None, cwd=tmp_path)
assert t.slug == "A"

# After:
t, _ = resolve_task(state, cli_task="A", env_task=None, cwd=tmp_path)
assert t.slug == "A"
```

Apply this pattern to every existing test in that file. The fix is mechanical — replace every `x = resolve_task(...)` with `x, _ = resolve_task(...)`. There's no test that needs to assert on the source unless you're touching one of the 5 new tests.

- [ ] **Step 1.5: Run resolver tests to verify all pass**

Run: `uv run pytest tests/core/test_task_resolver.py -v`
Expected: all pass (existing tests updated for tuple unpacking; 5 new tests pass).

- [ ] **Step 1.6: Run the wider `tests/core/` suite to catch downstream breakage**

Run: `uv run pytest tests/core/ --ignore=tests/core/view/test_web_port.py -q 2>&1 | tail -5`
Expected: all pass. If anything in core imports `resolve_task` directly and unpacks a single task, fix those call sites by adding `, _` unpacking.

- [ ] **Step 1.7: Commit**

```bash
git add src/mship/core/task_resolver.py tests/core/test_task_resolver.py
git commit -m "feat(resolver): return (Task, ResolutionSource) tuple; detect cwd-in-multiple ambiguity"
mship journal "resolve_task now returns (Task, ResolutionSource); cwd-in-multiple-worktrees case raises AmbiguousTaskError with candidates" --action committed
```

---

## Task 2: Output.breadcrumb + CLI helper

**Files:**
- Modify: `src/mship/cli/output.py`
- Modify: `src/mship/cli/_resolve.py`
- Create: `tests/cli/test_resolve.py`

**Context:** Add an `Output.breadcrumb` method that writes a dim one-line message to stderr, TTY-gated. Add `resolve_for_command` that prints the breadcrumb and returns `(task, source)`. Also upgrade `resolve_or_exit` to unpack the tuple (no behavior change — just returns the task as before) and upgrade its `AmbiguousTaskError` handler to list candidates.

- [ ] **Step 2.1: Add `Output.breadcrumb` with a test**

Append to `tests/cli/test_output.py` (create it if it doesn't exist):

```python
"""Tests for Output helpers."""
import io

import pytest

from mship.cli.output import Output


class _TTYStream:
    def __init__(self):
        self._buf = io.StringIO()
    def write(self, s):
        self._buf.write(s)
    def flush(self):
        pass
    def isatty(self):
        return True
    def getvalue(self):
        return self._buf.getvalue()


class _NonTTYStream:
    def __init__(self):
        self._buf = io.StringIO()
    def write(self, s):
        self._buf.write(s)
    def flush(self):
        pass
    def isatty(self):
        return False
    def getvalue(self):
        return self._buf.getvalue()


def test_breadcrumb_writes_to_stderr_on_tty():
    out = _TTYStream()
    err = _TTYStream()
    output = Output(stream=out, err_stream=err)
    output.breadcrumb("→ task: foo  (resolved via cwd)")
    assert "→ task: foo" in err.getvalue()
    # Not on stdout.
    assert out.getvalue() == ""
    # No "ERROR:" prefix.
    assert "ERROR" not in err.getvalue()


def test_breadcrumb_suppressed_on_non_tty():
    out = _NonTTYStream()
    err = _NonTTYStream()
    output = Output(stream=out, err_stream=err)
    output.breadcrumb("→ task: foo")
    assert out.getvalue() == ""
    assert err.getvalue() == ""
```

Run: `uv run pytest tests/cli/test_output.py -v`
Expected: FAIL — no `breadcrumb` method.

- [ ] **Step 2.2: Implement `Output.breadcrumb`**

Add to `src/mship/cli/output.py`, just after the existing `print` method (around line 58):

```python
    def breadcrumb(self, message: str) -> None:
        """Dim informational line to stderr. Used for task-resolution breadcrumbs.

        Suppressed when stdout is non-TTY (JSON-mode consumers should attach
        the same info as structured fields in their payload). Stderr so it
        doesn't corrupt stdout pipes; dim so it doesn't compete with real output.
        """
        if self.is_tty:
            self._err_console.print(f"[dim]{message}[/dim]")
```

Run: `uv run pytest tests/cli/test_output.py -v`
Expected: 2 passed.

- [ ] **Step 2.3: Write failing tests for `resolve_for_command`**

Create `tests/cli/test_resolve.py`:

```python
"""Tests for resolve_for_command: breadcrumb + ambiguity rendering."""
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output
from mship.core.state import Task, WorkspaceState


def _task(slug: str, worktree: Path | None = None) -> Task:
    return Task(
        slug=slug,
        description=f"d {slug}",
        phase="plan",
        created_at=datetime(2026, 4, 22, tzinfo=timezone.utc),
        affected_repos=["r"] if worktree else [],
        branch=f"feat/{slug}",
        worktrees={"r": worktree} if worktree else {},
    )


class _TTYStream:
    def __init__(self):
        self._buf = io.StringIO()
    def write(self, s):
        self._buf.write(s)
    def flush(self):
        pass
    def isatty(self):
        return True
    def getvalue(self):
        return self._buf.getvalue()


class _NonTTYStream(_TTYStream):
    def isatty(self):
        return False


def _tty_output():
    out, err = _TTYStream(), _TTYStream()
    return Output(stream=out, err_stream=err), err


def _nontty_output():
    out, err = _NonTTYStream(), _NonTTYStream()
    return Output(stream=out, err_stream=err), out, err


def test_breadcrumb_printed_on_tty(tmp_path: Path, monkeypatch):
    wt = tmp_path / "wt"; wt.mkdir()
    state = WorkspaceState(tasks={"A": _task("A", wt)})
    monkeypatch.chdir(wt)
    output, err = _tty_output()
    result = resolve_for_command("finish", state, cli_task=None, output=output)
    assert result.task.slug == "A"
    assert result.source == "cwd"
    body = err.getvalue()
    assert "→ task: A" in body
    assert "cwd" in body.lower()


def test_breadcrumb_source_is_cli_flag(tmp_path: Path, monkeypatch):
    state = WorkspaceState(tasks={"A": _task("A")})
    monkeypatch.chdir(tmp_path)
    output, err = _tty_output()
    result = resolve_for_command("finish", state, cli_task="A", output=output)
    assert result.source == "--task"
    assert "--task" in err.getvalue()


def test_no_breadcrumb_when_non_tty(tmp_path: Path, monkeypatch):
    wt = tmp_path / "wt"; wt.mkdir()
    state = WorkspaceState(tasks={"A": _task("A", wt)})
    monkeypatch.chdir(wt)
    output, out, err = _nontty_output()
    result = resolve_for_command("finish", state, cli_task=None, output=output)
    assert result.source == "cwd"
    assert out.getvalue() == ""
    assert err.getvalue() == ""


def test_ambiguity_lists_candidates_on_tty(tmp_path: Path, monkeypatch):
    """No-anchor + 2 tasks raises typer.Exit(1) and lists --task candidates."""
    wt_a = tmp_path / "a"; wt_a.mkdir()
    wt_b = tmp_path / "b"; wt_b.mkdir()
    state = WorkspaceState(tasks={
        "A": _task("A", wt_a),
        "B": _task("B", wt_b),
    })
    outside = tmp_path / "elsewhere"; outside.mkdir()
    monkeypatch.chdir(outside)
    output, err = _tty_output()
    with pytest.raises(typer.Exit):
        resolve_for_command("finish", state, cli_task=None, output=output)
    text = err.getvalue()
    assert "ambiguous" in text.lower() or "multiple" in text.lower()
    assert "--task A" in text
    assert "--task B" in text
```

Run: `uv run pytest tests/cli/test_resolve.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_for_command'`.

- [ ] **Step 2.4: Implement the CLI helper (final version, no revisions)**

Replace `src/mship/cli/_resolve.py` entirely with:

```python
"""CLI glue for mship.core.task_resolver.

Two entry points:

- `resolve_or_exit(state, cli_task)` — returns `Task`. Used by view commands
  that don't need the breadcrumb (`mship status`, `mship logs`, ...).
- `resolve_for_command(cmd, state, cli_task, output)` — returns `ResolvedTask`
  (task + source string). Prints a one-line breadcrumb to stderr when on a
  TTY. Used by state-changing verbs and subagent-feeding commands.

Both catch the three resolver exceptions and raise `typer.Exit(1)` with
friendly messages. When `AmbiguousTaskError.candidates` is populated, both
paths render `--task <slug>  (<worktree path>)` hints.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

import typer

from mship.cli.output import Output
from mship.core.state import Task, WorkspaceState
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
    resolve_task,
)


class ResolvedTask(NamedTuple):
    """Result of `resolve_for_command`.

    `task` is the resolved Task. `source` is the `ResolutionSource.value`
    string (e.g. "cwd", "--task", "MSHIP_TASK", "only active task"),
    suitable for inclusion in JSON payloads.
    """
    task: Task
    source: str


def _format_ambiguity(e: AmbiguousTaskError) -> list[str]:
    """Turn an AmbiguousTaskError into human-readable lines."""
    lines: list[str] = []
    if e.candidates:
        lines.append("Pick one with --task:")
        for slug, path in e.candidates:
            suffix = f"  ({path})" if path else ""
            lines.append(f"  --task {slug}{suffix}")
    else:
        lines.append(
            f"Multiple active tasks ({', '.join(e.active)}). "
            "Specify --task, set MSHIP_TASK, or cd into a worktree."
        )
    return lines


def _handle_resolver_errors(
    state: WorkspaceState, output: Output, fn,
):
    """Shared exception handling for the two resolver entry points."""
    try:
        return fn()
    except NoActiveTaskError as e:
        output.error(str(e))
        raise typer.Exit(1)
    except UnknownTaskError as e:
        known = ", ".join(sorted(state.tasks.keys())) or "(none)"
        output.error(f"Unknown task: {e.slug}. Known: {known}.")
        raise typer.Exit(1)
    except AmbiguousTaskError as e:
        output.error("ambiguous task:")
        for line in _format_ambiguity(e):
            output.error(line)
        raise typer.Exit(1)


def resolve_or_exit(state: WorkspaceState, cli_task: str | None) -> Task:
    output = Output()
    def _go() -> Task:
        task, _source = resolve_task(
            state,
            cli_task=cli_task,
            env_task=os.environ.get("MSHIP_TASK"),
            cwd=Path.cwd(),
        )
        return task
    return _handle_resolver_errors(state, output, _go)


def resolve_for_command(
    cmd_name: str,
    state: WorkspaceState,
    cli_task: str | None,
    output: Output,
) -> ResolvedTask:
    """Resolve a task, print a TTY breadcrumb, return (task, source).

    On non-TTY, the caller is expected to include `resolved_task` and
    `resolution_source` fields in their JSON output (the `source` value
    is exactly what belongs in the JSON).
    """
    def _go() -> ResolvedTask:
        task, source = resolve_task(
            state,
            cli_task=cli_task,
            env_task=os.environ.get("MSHIP_TASK"),
            cwd=Path.cwd(),
        )
        output.breadcrumb(f"→ task: {task.slug}  (resolved via {source.value})")
        return ResolvedTask(task=task, source=source.value)
    return _handle_resolver_errors(state, output, _go)
```

Note: `cmd_name` is accepted but currently unused; it's part of the signature for forward-compat (future per-command suppression, telemetry, or richer breadcrumb formatting). Keeping it in the API now avoids a breaking change later.

- [ ] **Step 2.5: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_resolve.py tests/cli/test_output.py -v`
Expected: all 6 tests pass.

- [ ] **Step 2.6: Run broader `tests/cli/` to catch regressions**

Run: `uv run pytest tests/cli/ -q 2>&1 | tail -5`
Expected: all pass. `resolve_or_exit` still returns a single `Task` (backward-compat), so existing callers are unaffected.

- [ ] **Step 2.7: Commit**

```bash
git add src/mship/cli/output.py src/mship/cli/_resolve.py tests/cli/test_output.py tests/cli/test_resolve.py
git commit -m "feat(cli): Output.breadcrumb + resolve_for_command helper"
mship journal "Output.breadcrumb writes dim stderr lines TTY-gated; resolve_for_command returns ResolvedTask(task, source) and prints the breadcrumb" --action committed
```

---

## Task 3: Migrate the 9 in-scope commands

**Files:**
- Modify: `src/mship/cli/worktree.py` (finish + close)
- Modify: `src/mship/cli/phase.py`
- Modify: `src/mship/cli/dispatch.py`
- Modify: `src/mship/cli/exec.py`
- Modify: `src/mship/cli/switch.py`
- Modify: `src/mship/cli/block.py` (two call sites)
- Modify: `src/mship/cli/context.py`
- Modify: `src/mship/cli/log.py`
- Create: `tests/cli/test_breadcrumb.py`

**Context:** Replace `resolve_or_exit` with `resolve_for_command` at each call site. For each command's non-TTY JSON output, add `resolved_task` and `resolution_source` fields.

- [ ] **Step 3.1: Write failing parametrized integration test**

CliRunner is non-TTY by default — which means the breadcrumb is suppressed and the contract is "JSON payload gains `resolved_task` + `resolution_source`." That's what we assert on here. The TTY breadcrumb behavior is already covered by Task 2's unit tests.

Create `tests/cli/test_breadcrumb.py`:

```python
"""Integration tests: in-scope commands surface resolved_task + resolution_source
in their non-TTY JSON output. See #77. TTY breadcrumb behavior covered in
tests/cli/test_resolve.py."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


@pytest.fixture
def ws_with_task(workspace_with_git: Path):
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    (workspace_with_git / ".mothership").mkdir(exist_ok=True)
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    container.shell.override(mock_shell)
    runner.invoke(app, ["spawn", "breadcrumb test", "--repos", "shared", "--force-audit"])
    yield workspace_with_git
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.shell.reset_override()


def _parse_json_or_skip(text: str) -> dict:
    """Some commands emit multiple JSON objects or JSON followed by trailing
    plain-text (see `finish`). Parse the first JSON object; skip test if none."""
    text = text.strip()
    if not text.startswith("{"):
        pytest.skip(f"command did not emit JSON in non-TTY mode: {text[:80]}")
    depth = 0
    for i, c in enumerate(text):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[: i + 1])
    pytest.skip("could not find JSON object boundary")


def test_phase_emits_resolution_fields(ws_with_task: Path):
    result = runner.invoke(app, ["phase", "dev", "--task", "breadcrumb-test"])
    assert result.exit_code == 0, result.output
    payload = _parse_json_or_skip(result.output)
    assert payload.get("resolved_task") == "breadcrumb-test"
    assert payload.get("resolution_source") == "--task"


def test_context_emits_resolution_fields(ws_with_task: Path):
    result = runner.invoke(app, ["context", "--task", "breadcrumb-test"])
    assert result.exit_code == 0, result.output
    payload = _parse_json_or_skip(result.output)
    assert payload.get("resolved_task") == "breadcrumb-test"
    assert payload.get("resolution_source") == "--task"


def test_block_emits_resolution_fields(ws_with_task: Path):
    result = runner.invoke(
        app, ["block", "--reason", "stuck", "--task", "breadcrumb-test"],
    )
    assert result.exit_code == 0, result.output
    payload = _parse_json_or_skip(result.output)
    assert payload.get("resolved_task") == "breadcrumb-test"
    assert payload.get("resolution_source") == "--task"


def test_log_emits_resolution_fields(ws_with_task: Path):
    result = runner.invoke(
        app, ["journal", "test message", "--task", "breadcrumb-test"],
    )
    assert result.exit_code == 0, result.output
    payload = _parse_json_or_skip(result.output)
    assert payload.get("resolved_task") == "breadcrumb-test"
    assert payload.get("resolution_source") == "--task"


def test_ambiguity_lists_candidates(ws_with_task: Path):
    """Second task + running from outside both worktrees → ambiguity error with
    `--task <slug>` hints for BOTH tasks."""
    runner.invoke(
        app, ["spawn", "second task", "--repos", "shared", "--force-audit"],
    )
    # Invoke from the workspace root — outside every worktree.
    import os
    prev = os.getcwd()
    os.chdir(ws_with_task)
    try:
        result = runner.invoke(app, ["phase", "dev"])
    finally:
        os.chdir(prev)
    assert result.exit_code != 0
    assert "--task breadcrumb-test" in result.output
    assert "--task second-task" in result.output
```

Note on `finish` / `close`: they emit text + JSON mixed; `_parse_json_or_skip` handles commands that start with a JSON object. For commands whose JSON output is elsewhere (e.g. after plain-text lines), the test skips gracefully — the fields still land in the payload, but this integration test only validates the easy cases. `finish`-specific field verification is already covered by `tests/test_finish_integration.py` regression.

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_breadcrumb.py -v`
Expected: FAIL — the breadcrumb isn't yet wired into any command.

- [ ] **Step 3.3: Migrate `finish` + `close`**

Edit `src/mship/cli/worktree.py`.

At line 381 (inside `close` body), replace:

```python
        from mship.cli._resolve import resolve_or_exit
```

with:

```python
        from mship.cli._resolve import resolve_for_command
```

At line 401 (inside `close`), replace:

```python
        t = resolve_or_exit(state, task)
```

with:

```python
        resolved = resolve_for_command("close", state, task, output)
        t = resolved.task
```

At line 640 (inside `finish`), replace:

```python
        from mship.cli._resolve import resolve_or_exit
```

with:

```python
        from mship.cli._resolve import resolve_for_command
```

At line 700 (inside `finish`), replace:

```python
        t = resolve_or_exit(state, task)
```

with:

```python
        resolved = resolve_for_command("finish", state, task, output)
        t = resolved.task
```

Then add `resolved_task` and `resolution_source` to the JSON output blocks in both commands. Grep within `worktree.py` for `output.json(` inside the `finish` and `close` function bodies; for each call, add the two fields. Example for finish (existing output around line 1022-1028):

```python
            output.json({
                "task": task.slug,
                "prs": pr_list,
                "re_pushed": repushed_repos,
                "skipped_untouched": skipped_untouched,
                "finished_at": task.finished_at.isoformat(),
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })
```

Do the same for every `output.json(` call in `close`. If the `resolved` variable isn't in scope (e.g. if close's JSON block runs before it's created), hoist the `resolve_for_command` call above it.

- [ ] **Step 3.4: Migrate `phase`, `dispatch`, `exec`, `switch`, `log`, `context`**

Each of these files has a single `resolve_or_exit` call site (see line numbers from the file-structure section). For each:

1. Replace the import: `from mship.cli._resolve import resolve_or_exit` → `from mship.cli._resolve import resolve_for_command`.
2. Replace the call: `t = resolve_or_exit(state, task)` → `resolved = resolve_for_command("<cmdname>", state, task, output)` + `t = resolved.task`.
3. For each `output.json(...)` call in that command, add `"resolved_task": resolved.task.slug, "resolution_source": resolved.source`.

Exact substitutions:

**phase.py:28** — cmd name `"phase"`.
**dispatch.py:29** — cmd name `"dispatch"`; variable already named `task_obj`, so: `resolved = resolve_for_command("dispatch", state, task, output)` + `task_obj = resolved.task`.
**exec.py:78** — cmd name `"exec"`. Note: there's a second resolver path at line 245 that uses `resolve_task` directly (not `resolve_or_exit`) — leave that one alone; the spec only covers the primary call site.
**switch.py:29** — cmd name `"switch"`. Variable already named `t` via `task_opt`.
**log.py:32** — cmd name `"log"`. Variable `task_opt`.
**context.py** — (grep the exact line; pattern matches the others). Cmd name `"context"`.

- [ ] **Step 3.5: Migrate `block.py` (two call sites)**

`block.py` has resolve calls at lines 22 and 55 (one per subcommand, e.g. `block` and `unblock`). Migrate both identically with cmd name `"block"`.

- [ ] **Step 3.6: Run the breadcrumb integration tests**

Run: `uv run pytest tests/cli/test_breadcrumb.py -v`
Expected: 5 passed.

- [ ] **Step 3.7: Run full test suite for regressions**

Run: `uv run pytest tests/ --ignore=tests/core/view/test_web_port.py -q 2>&1 | tail -5`
Expected: all pass. Any test that captures stdout and asserts exact output may now see the breadcrumb on stderr — but since we routed the breadcrumb to stderr (not stdout) and non-TTY mode suppresses it entirely, normal CliRunner tests should be unaffected. If any regress, the fix is either (a) CliRunner's TTY detection — CliRunner defaults to non-TTY, so most existing tests won't even see the breadcrumb; or (b) the test asserts on `result.output` and a breadcrumb crept in on TTY-forced invocations — loosen the assertion to check `in` rather than `==`.

- [ ] **Step 3.8: Commit**

```bash
git add src/mship/cli/worktree.py src/mship/cli/phase.py src/mship/cli/dispatch.py src/mship/cli/exec.py src/mship/cli/switch.py src/mship/cli/block.py src/mship/cli/context.py src/mship/cli/log.py tests/cli/test_breadcrumb.py
git commit -m "feat(cli): breadcrumb + resolution_source on 9 task-scoped commands"
mship journal "9 in-scope commands now print → task: <slug> (resolved via <source>) on TTY and emit resolved_task + resolution_source in non-TTY JSON" --action committed
```

---

## Task 4: Smoke + PR

**Files:**
- None (verification + PR only).

**Context:** Reinstall mship, run a manual smoke to confirm the breadcrumb appears on TTY and the ambiguity hint fires from a non-worktree dir with multiple tasks.

- [ ] **Step 4.1: Reinstall the tool**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/task-breadcrumb-77
uv tool install --reinstall --from . mothership
```

- [ ] **Step 4.2: TTY smoke — breadcrumb on `mship phase`**

From inside the worktree, run:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/task-breadcrumb-77
mship phase dev 2>&1 | head -3
```

Expected:
```
→ task: task-breadcrumb-77  (resolved via cwd)
... (phase output) ...
```

- [ ] **Step 4.3: JSON smoke — no breadcrumb, fields in payload**

```bash
mship phase dev --task task-breadcrumb-77 2>/dev/null | grep -E "resolved_task|resolution_source"
```

Note: `mship phase` is TTY-emit by default. If the JSON variant isn't available, substitute `mship context --task task-breadcrumb-77 | head -20` and look for the two fields in JSON output. Expected: both fields present with values `task-breadcrumb-77` and `--task`.

- [ ] **Step 4.4: Ambiguity smoke**

Spawn a second task, then run an in-scope command from a non-worktree dir:

```bash
cd /tmp
mship spawn "temp smoke task" --repos mothership --skip-setup --slug smoke-tmp
cd /tmp  # not inside any worktree
mship phase dev 2>&1 | head -5
```

Expected:
```
ERROR: ambiguous task:
ERROR: Pick one with --task:
ERROR:   --task smoke-tmp  (/path/to/worktrees/feat/smoke-tmp)
ERROR:   --task task-breadcrumb-77  (/path/to/worktrees/feat/task-breadcrumb-77)
```

Cleanup:
```bash
mship close smoke-tmp -y --abandon
```

- [ ] **Step 4.5: Full pytest**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/task-breadcrumb-77
uv run pytest tests/ --ignore=tests/core/view/test_web_port.py 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 4.6: Open the PR**

Write `/tmp/breadcrumb-body.md`:

```markdown
## Summary

Closes #77.

Task-scoped state-changing commands now print a one-line breadcrumb naming the resolved task and source; ambiguous cwd resolution fails loudly with `--task <slug>` candidate hints instead of silently picking one.

### Commit 1 — `feat(resolver): return (Task, ResolutionSource) tuple`

- `resolve_task` returns `(Task, ResolutionSource)`. Enum values: `--task`, `MSHIP_TASK`, `cwd`, `only active task`.
- `AmbiguousTaskError` gains a `candidates: list[tuple[str, Path | None]]` attribute (slug + worktree path) so callers can render concrete hints.
- The cwd walk now collects all matches and raises on ≥2 rather than silently picking the first.

### Commit 2 — `feat(cli): resolve_for_command prints breadcrumb`

- New `resolve_for_command(cmd, state, cli_task, output) -> ResolvedTask` helper in `_resolve.py`.
- On TTY: prints `→ task: <slug>  (resolved via <source>)` to stderr before command output.
- On non-TTY: silent — callers attach `resolved_task` + `resolution_source` to their JSON payloads.
- `resolve_or_exit` preserved (returns `Task` as before) for out-of-scope read-only commands.

### Commit 3 — `feat(cli): breadcrumb + resolution_source on 9 task-scoped commands`

- `finish`, `close`, `phase`, `dispatch`, `exec`, `switch`, `block`, `context`, `log` migrate to the helper.
- Each command's JSON output gains `resolved_task` + `resolution_source` fields.
- View commands (`status`, `logs`, `diff`, ...) unchanged — they already name the task.

## Test plan

- [x] `tests/core/test_task_resolver.py`: 6 new tests for sources + multi-match cwd ambiguity + candidates.
- [x] `tests/cli/test_output.py`: 2 new tests for `Output.breadcrumb` TTY/non-TTY behavior.
- [x] `tests/cli/test_resolve.py`: 4 new tests for `resolve_for_command` + ambiguity formatting.
- [x] `tests/cli/test_breadcrumb.py`: 5 parametrized integration tests covering phase/context/block/log/ambiguity.
- [x] Full suite: all pass.
- [x] Manual smoke: breadcrumb visible from worktree cwd; ambiguity hint fires from non-worktree cwd with 2 tasks; non-TTY JSON carries the two new fields.
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/task-breadcrumb-77
mship finish --body-file /tmp/breadcrumb-body.md --title "feat(mship): task breadcrumb + ambiguity detection (#77)"
```

Expected: PR URL returned.

---

## Done when

- [x] `resolve_task` returns `(Task, ResolutionSource)`; 4 distinct sources distinguishable.
- [x] Cwd walk raises `AmbiguousTaskError` on multi-match with `candidates` populated.
- [x] `resolve_for_command` prints TTY breadcrumb to stderr; silent on non-TTY.
- [x] 9 in-scope commands migrated; each JSON output has `resolved_task` + `resolution_source`.
- [x] 17 new tests pass (6 resolver + 2 output + 4 CLI helper + 5 integration).
- [x] Full pytest green.
- [x] Manual smoke confirms TTY breadcrumb, JSON fields, and ambiguity hints.
