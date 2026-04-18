# View Watch-Mode No-Task Tolerance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `mship view journal --watch` and `mship view spec --watch` mount successfully when the workspace has no resolvable task, render a short placeholder, and re-resolve on every tick so the pane automatically picks up a task once one becomes resolvable.

**Architecture:** In watch mode, each view re-runs `resolve_task()` per tick. Resolver errors (`NoActiveTaskError`, `AmbiguousTaskError`, `UnknownTaskError`) map to placeholder strings via a single new helper module; the view swaps the placeholder in place of the normal body and keeps polling. Non-watch mode is unchanged — the CLI still calls `resolve_or_exit()` and exits 1 on failure.

**Tech Stack:** Python 3.14, Typer CLI, Textual TUI, pytest-asyncio, Textual `App.run_test()` pilot for in-process view tests.

**Reference spec:** `docs/superpowers/specs/2026-04-18-view-watch-no-task-design.md`

---

## File structure

**New files:**
- `src/mship/cli/view/_placeholders.py` — `placeholder_for(err)` helper mapping resolver exceptions to placeholder strings.
- `tests/cli/view/test_placeholders.py` — unit tests for the helper.
- `tests/cli/view/test_view_cli.py` — CLI-level regression tests for non-watch exit-1 contract and watch-mode mount tolerance.

**Modified files:**
- `src/mship/cli/view/logs.py` — `LogsView` constructor gains `cli_task`, `cwd`; new `_resolve_slug()`; `gather()` catches resolver errors and returns placeholder. CLI handler branches on `watch`.
- `src/mship/cli/view/spec.py` — `SpecView` constructor gains `state_manager`, `cli_task`, `cwd` (replacing the pre-loaded `state` param's role); `_refresh_content()` reloads state and resolves per tick; `_render_task_fallback` takes `slug` + `state` as parameters. CLI handler branches on `watch`.
- `tests/cli/view/test_logs_view.py` — new watch-mode tolerance tests.
- `tests/cli/view/test_spec_view.py` — new watch-mode tolerance tests.

**Task ordering rationale:** Task 1 (helper) has no dependencies. Tasks 2–3 land the journal fix end-to-end. Tasks 4–6 land the spec fix end-to-end. Task 7 verifies and ships. Journal before spec because `LogsView` is simpler (single widget, single state-manager reference) and validates the pattern.

---

## Task 1: Placeholder helper module

**Files:**
- Create: `src/mship/cli/view/_placeholders.py`
- Create: `tests/cli/view/test_placeholders.py`

- [ ] **Step 1.1: Write failing tests**

Write `tests/cli/view/test_placeholders.py`:

```python
import pytest

from mship.cli.view._placeholders import placeholder_for
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
)


def test_no_active_task_placeholder():
    err = NoActiveTaskError()
    text = placeholder_for(err)
    assert "No active task" in text
    assert "mship spawn" in text


def test_ambiguous_placeholder_lists_slugs():
    err = AmbiguousTaskError(active=["alpha", "beta"])
    text = placeholder_for(err)
    assert "Multiple active tasks" in text
    assert "alpha" in text
    assert "beta" in text
    assert "--task" in text
    assert "MSHIP_TASK" in text


def test_unknown_slug_placeholder_names_slug():
    err = UnknownTaskError(slug="missing-one")
    text = placeholder_for(err)
    assert "missing-one" in text
    assert "Waiting" in text or "not found" in text


def test_unknown_exception_type_reraised():
    class _Other(Exception):
        pass
    with pytest.raises(_Other):
        placeholder_for(_Other("oops"))
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `pytest tests/cli/view/test_placeholders.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mship.cli.view._placeholders'`.

- [ ] **Step 1.3: Create the helper module**

Write `src/mship/cli/view/_placeholders.py`:

```python
"""Map task-resolver exceptions to short placeholder strings used by views
in --watch mode when a task cannot be resolved yet.

Centralising the strings here lets tests assert against the same source of
wording the views render, avoiding copy drift between implementation and
tests.
"""
from __future__ import annotations

from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
)


def placeholder_for(err: Exception) -> str:
    if isinstance(err, NoActiveTaskError):
        return 'No active task. Run `mship spawn "description"` to start one.'
    if isinstance(err, AmbiguousTaskError):
        return (
            f"Multiple active tasks ({', '.join(err.active)}). "
            "Pass --task, set MSHIP_TASK, or close extras."
        )
    if isinstance(err, UnknownTaskError):
        return f"Task '{err.slug}' not found. Waiting for it to be spawned."
    raise err
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `pytest tests/cli/view/test_placeholders.py -v`
Expected: 4 passed.

- [ ] **Step 1.5: Commit (pair with `mship journal`)**

```bash
git add src/mship/cli/view/_placeholders.py tests/cli/view/test_placeholders.py
git commit -m "feat(view): placeholder_for() helper for watch-mode resolver errors"
mship journal "placeholder_for helper + unit tests for all three resolver errors" --action committed
```

---

## Task 2: `LogsView` — watch-mode resolver + placeholder render

**Files:**
- Modify: `src/mship/cli/view/logs.py`
- Modify: `tests/cli/view/test_logs_view.py`

**Context:** Today `LogsView.gather()` reads a pre-resolved `task_slug` from its constructor. To tolerate unresolvable state in watch mode, it gains two new kwargs (`cli_task`, `cwd`), a `_resolve_slug()` method that re-runs `resolve_task()` each call, and a `try/except` around the resolver errors that returns `placeholder_for(err)`. Non-watch keeps the pre-resolved path via a non-None `task_slug`.

- [ ] **Step 2.1: Write failing tests**

Add to `tests/cli/view/test_logs_view.py` (append after existing tests; keep existing imports and fakes intact):

```python
# --- watch-mode resolver tolerance ---

from pathlib import Path as _Path
from dataclasses import dataclass as _dataclass, field as _field


@_dataclass
class _FakeTask:
    slug: str
    active_repo: str | None = None
    worktrees: dict = _field(default_factory=dict)


class _FakeStateWithTasks:
    def __init__(self, tasks_dict):
        self.tasks = tasks_dict


class _MutableStateMgr:
    """State manager whose returned state can be changed between ticks."""
    def __init__(self, tasks_dict=None):
        self._tasks = tasks_dict or {}

    def set_tasks(self, tasks_dict):
        self._tasks = tasks_dict

    def load(self):
        return _FakeStateWithTasks(self._tasks)


@pytest.mark.asyncio
async def test_logs_view_watch_no_active_task_shows_placeholder(tmp_path):
    mgr = _MutableStateMgr(tasks_dict={})
    view = LogsView(
        state_manager=mgr,
        log_manager=_FakeLogMgr([]),
        task_slug=None,
        cli_task=None,
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "No active task" in text


@pytest.mark.asyncio
async def test_logs_view_watch_ambiguous_shows_placeholder(tmp_path):
    mgr = _MutableStateMgr(tasks_dict={
        "alpha": _FakeTask("alpha"),
        "beta":  _FakeTask("beta"),
    })
    view = LogsView(
        state_manager=mgr,
        log_manager=_FakeLogMgr([]),
        task_slug=None,
        cli_task=None,
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "Multiple active tasks" in text
        assert "alpha" in text and "beta" in text


@pytest.mark.asyncio
async def test_logs_view_watch_unknown_slug_shows_placeholder(tmp_path):
    mgr = _MutableStateMgr(tasks_dict={"other": _FakeTask("other")})
    view = LogsView(
        state_manager=mgr,
        log_manager=_FakeLogMgr([]),
        task_slug=None,
        cli_task="missing-one",
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "missing-one" in text


@pytest.mark.asyncio
async def test_logs_view_watch_transitions_from_placeholder_to_entries(tmp_path):
    entries = [
        _Entry(datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc), "first entry"),
    ]
    mgr = _MutableStateMgr(tasks_dict={})
    view = LogsView(
        state_manager=mgr,
        log_manager=_FakeLogMgr(entries),
        task_slug=None,
        cli_task=None,
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No active task" in view.rendered_text()
        mgr.set_tasks({"solo": _FakeTask("solo")})
        # Force a refresh (rather than wait for the 0.5s interval) so the test
        # is deterministic on slow CI.
        view._refresh_content()
        await pilot.pause()
        text = view.rendered_text()
        assert "first entry" in text
        assert "No active task" not in text


@pytest.mark.asyncio
async def test_logs_view_non_watch_with_task_slug_does_not_call_resolver(tmp_path):
    """Regression: non-watch path stays pre-resolved, does not touch the resolver."""
    class _BlowUpStateMgr:
        def load(self):  # pragma: no cover - should never be called
            raise AssertionError("resolver must not be called in non-watch path")

    entries = [_Entry(datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc), "ok")]
    view = LogsView(
        state_manager=_BlowUpStateMgr(),
        log_manager=_FakeLogMgr(entries),
        task_slug="pre-resolved",
        cli_task=None,
        cwd=tmp_path,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "ok" in view.rendered_text()
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `pytest tests/cli/view/test_logs_view.py -v`
Expected: 5 FAILs — new tests fail because `LogsView.__init__` does not yet accept `cli_task` / `cwd` and `gather()` does not handle resolver errors.

- [ ] **Step 2.3: Update `LogsView` — constructor, `_resolve_slug`, `gather`**

Edit `src/mship/cli/view/logs.py`. Replace the module with:

```python
import os
from pathlib import Path
from typing import Optional

import typer

from mship.cli.view._base import ViewApp
from mship.cli.view._placeholders import placeholder_for
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
    resolve_task,
)


class LogsView(ViewApp):
    def __init__(
        self,
        state_manager,
        log_manager,
        task_slug: Optional[str],
        scope_to_repo: Optional[str] = None,
        *,
        all_: bool = False,
        cli_task: Optional[str] = None,
        cwd: Optional[Path] = None,
        **kw,
    ):
        super().__init__(**kw)
        self._state_manager = state_manager
        self._log_manager = log_manager
        self._task_slug = task_slug
        self._scope_to_repo = scope_to_repo
        self._all = all_
        self._cli_task = cli_task
        self._cwd = cwd if cwd is not None else Path.cwd()

    def _resolve_slug(self) -> str:
        """Return the task slug to render for this tick.

        Non-watch: returns the pre-resolved `task_slug` passed in by the CLI.
        Watch: re-runs `resolve_task()` each call; resolver errors propagate.
        """
        if self._task_slug is not None:
            return self._task_slug
        state = self._state_manager.load()
        task = resolve_task(
            state,
            cli_task=self._cli_task,
            env_task=os.environ.get("MSHIP_TASK"),
            cwd=self._cwd,
        )
        return task.slug

    def gather(self) -> str:
        try:
            slug = self._resolve_slug()
        except (NoActiveTaskError, AmbiguousTaskError, UnknownTaskError) as err:
            return placeholder_for(err)

        scope = self._scope_to_repo
        # Watch mode re-reads state per tick so scoping follows `mship switch`.
        # Non-watch trusts the CLI-precomputed `scope_to_repo`. `--all` skips
        # per-tick scoping regardless of mode.
        if self._task_slug is None and not self._all:
            state = self._state_manager.load()
            task = state.tasks.get(slug)
            if task is not None:
                scope = getattr(task, "active_repo", None)

        entries = self._log_manager.read(slug)
        if scope is not None:
            entries = [e for e in entries if e.repo is None or e.repo == scope]
        if not entries:
            return f"Log for {slug} is empty"
        lines = []
        for entry in entries:
            ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            meta_parts: list[str] = []
            if entry.repo:
                meta_parts.append(f"repo={entry.repo}")
            if entry.iteration is not None:
                meta_parts.append(f"iter={entry.iteration}")
            if entry.test_state:
                meta_parts.append(f"test={entry.test_state}")
            if entry.action:
                meta_parts.append(f"action={entry.action}")
            meta = f"  [{' '.join(meta_parts)}]" if meta_parts else ""
            lines.append(f"{ts}{meta}")
            lines.append(f"  {entry.message}")
            if entry.open_question:
                lines.append(f"  ⚠ open: {entry.open_question}")
        return "\n".join(lines)


def register(app: typer.Typer, get_container):
    @app.command(name="journal")
    def journal(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var."),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        all_: bool = typer.Option(False, "--all", help="Show all log entries, ignore active_repo"),
    ):
        """Live tail of a task's journal."""
        container = get_container()

        if watch:
            # Watch mode: defer task resolution into the view so resolver
            # errors become placeholder text instead of exit-1.
            task_slug: Optional[str] = None
            cli_task = task
            scope: Optional[str] = None
        else:
            from mship.cli._resolve import resolve_or_exit
            state = container.state_manager().load()
            t = resolve_or_exit(state, task)
            task_slug = t.slug
            cli_task = None
            scope = None
            if not all_ and t.active_repo is not None:
                scope = t.active_repo

        view = LogsView(
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            task_slug=task_slug,
            scope_to_repo=scope,
            all_=all_,
            cli_task=cli_task,
            cwd=Path.cwd(),
            watch=watch,
            interval=interval,
        )
        view.run()
```

Key changes vs. previous version:
1. New imports (`os`, `Path`, `placeholder_for`, resolver errors, `resolve_task`).
2. Constructor adds `all_`, `cli_task`, `cwd` kwargs with defaults.
3. `_resolve_slug` replaces the old one-liner.
4. `gather()` wraps slug resolution in `try/except` and maps to placeholder.
5. `gather()` re-reads `active_repo` each tick **only in watch mode and when `--all` is false**; non-watch trusts the CLI's pre-computed `scope_to_repo`. This keeps the non-watch regression test (which uses a state manager that blows up when called) from tripping.
6. CLI handler splits on `watch`: non-watch calls `resolve_or_exit`, watch passes `cli_task=task`.

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `pytest tests/cli/view/test_logs_view.py -v`
Expected: all tests pass (5 new + existing).

- [ ] **Step 2.5: Run full view test subdir**

Run: `pytest tests/cli/view/ -v`
Expected: all green.

- [ ] **Step 2.6: Commit**

```bash
git add src/mship/cli/view/logs.py tests/cli/view/test_logs_view.py
git commit -m "feat(view): LogsView tolerates unresolved task in --watch"
mship journal "LogsView re-resolves each tick in watch mode; placeholder on resolver error" --action committed
```

---

## Task 3: CLI smoke — `mship view journal`

**Files:**
- Create: `tests/cli/view/test_view_cli.py`

**Context:** Regression-guard the non-watch exit-1 contract at the CLI runner level, and assert that the watch-mode entry point does *not* exit 1 when no task is present. Mounting a Textual app inside `CliRunner` is avoided by stopping before the app runs — we use `monkeypatch` to replace `LogsView.run` with a no-op so the handler executes its branch logic without launching the TUI.

- [ ] **Step 3.1: Write failing tests**

Write `tests/cli/view/test_view_cli.py`:

```python
"""CLI-level regression tests for view watch-mode tolerance.

These tests avoid actually launching Textual apps by patching the view's
`.run()` method to a no-op. The goal is to assert that the CLI handler's
branching logic (watch vs. non-watch) produces the right exit code and
constructor arguments, not to exercise the Textual render loop (which is
covered by the view-level tests).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, WorkspaceState


def _empty_workspace(tmp_path: Path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    StateManager(state_dir).save(WorkspaceState(tasks={}))
    return cfg, state_dir


@pytest.fixture
def empty_workspace(tmp_path, monkeypatch):
    cfg, state_dir = _empty_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    yield tmp_path
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()


def test_journal_non_watch_no_task_exits_1(empty_workspace):
    runner = CliRunner()
    result = runner.invoke(app, ["view", "journal"])
    assert result.exit_code == 1
    assert "no active task" in (result.output or "").lower()


def test_journal_watch_no_task_constructs_view_without_exit(empty_workspace, monkeypatch):
    from mship.cli.view import logs as logs_mod
    captured = {}

    def _fake_run(self):
        # Capture the instance so we can assert on its state.
        captured["view"] = self

    monkeypatch.setattr(logs_mod.LogsView, "run", _fake_run)
    runner = CliRunner()
    result = runner.invoke(app, ["view", "journal", "--watch"])
    assert result.exit_code == 0, result.output
    view = captured["view"]
    assert view._task_slug is None
    assert view._cli_task is None
    assert view._watch is True


def test_journal_watch_with_unknown_task_constructs_view_without_exit(empty_workspace, monkeypatch):
    from mship.cli.view import logs as logs_mod
    captured = {}
    monkeypatch.setattr(logs_mod.LogsView, "run", lambda self: captured.setdefault("view", self))
    runner = CliRunner()
    result = runner.invoke(app, ["view", "journal", "--watch", "--task", "missing"])
    assert result.exit_code == 0, result.output
    view = captured["view"]
    assert view._task_slug is None
    assert view._cli_task == "missing"
```

- [ ] **Step 3.2: Run tests to verify they pass**

Run: `pytest tests/cli/view/test_view_cli.py -v`
Expected: 3 passed. (Task 2 already wired the CLI handler, so these should pass immediately — they exist as regression guards.)

- [ ] **Step 3.3: Commit**

```bash
git add tests/cli/view/test_view_cli.py
git commit -m "test(view): cli-level regression for journal --watch no-task tolerance"
mship journal "cli runner tests for journal non-watch exit-1 and watch tolerance" --action committed
```

---

## Task 4: `SpecView` refactor — `state_manager`, `cli_task`, `cwd`; `_render_task_fallback` takes `slug` + `state`

**Files:**
- Modify: `src/mship/cli/view/spec.py`
- Modify: `tests/cli/view/test_spec_view.py` (only if existing tests break on signature change)

**Context:** Before we can add per-tick resolution, `SpecView` needs to refresh state on each refresh rather than reading a pre-loaded `self._state`. This task is a pure refactor: add new kwargs, route the fallback signature through a state parameter, leave behavior unchanged for the "explicit spec name" path. No new behavior — existing tests must continue to pass.

- [ ] **Step 4.1: Edit `SpecView` constructor and `_render_task_fallback`**

Apply these edits to `src/mship/cli/view/spec.py`.

Replace the `SpecView` class body (lines 20–133 of the current file) with:

```python
class SpecView(ViewApp):
    def __init__(
        self,
        workspace_root: Path,
        name_or_path: Optional[str],
        *,
        task: Optional[str] = None,
        state_manager=None,
        state=None,
        log_manager=None,
        cli_task: Optional[str] = None,
        cwd: Optional[Path] = None,
        **kw,
    ):
        # Strip SpecView-specific kwargs before passing to super
        for k in ("workspace_root", "name_or_path", "task",
                  "state_manager", "state", "log_manager",
                  "cli_task", "cwd"):
            kw.pop(k, None)
        super().__init__(**kw)
        self._workspace_root = workspace_root
        self._name_or_path = name_or_path
        self._task_filter = task
        self._state_manager = state_manager
        self._initial_state = state  # kept for existing tests that pre-load state
        self._log_manager = log_manager
        self._cli_task = cli_task
        self._cwd = cwd if cwd is not None else Path.cwd()
        self._markdown: Markdown | None = None
        self._error_static: Static | None = None
        self._body: VerticalScroll | None = None
        self._last_source: str = ""
        self._last_error: str = ""

    def compose(self) -> ComposeResult:
        self._markdown = Markdown("")
        self._error_static = Static("", expand=True)
        self._body = VerticalScroll(self._markdown, self._error_static)
        yield self._body

    def gather(self) -> str:  # not used; refresh is overridden directly
        return ""

    def _current_state(self):
        """Return fresh state from state_manager if available; else the
        pre-loaded state passed in at construction (kept for unit tests)."""
        if self._state_manager is not None:
            return self._state_manager.load()
        return self._initial_state

    def _refresh_content(self) -> None:
        assert self._markdown is not None
        assert self._error_static is not None
        assert self._body is not None
        was_at_end = self._body.scroll_y >= (self._body.max_scroll_y - 1)
        prev_y = self._body.scroll_y

        state = self._current_state()
        try:
            path = find_spec(self._workspace_root, self._name_or_path, task=self._task_filter, state=state)
            source = path.read_text()
            self._last_source = source
            self._last_error = ""
            self._markdown.update(source)
            self._error_static.update("")
        except SpecNotFoundError as e:
            if self._name_or_path is None:
                body = self._render_task_fallback(self._task_filter, state, default_error=str(e))
                self._last_source = body
                self._last_error = ""
                self._markdown.update(body)
                self._error_static.update("")
            else:
                error_msg = f"Spec not found: {e}"
                self._last_source = ""
                self._last_error = error_msg
                self._markdown.update("")
                self._error_static.update(error_msg)
        self.call_after_refresh(self._restore_scroll, prev_y, was_at_end)

    def _render_task_fallback(self, slug: Optional[str], state, *, default_error: str) -> str:
        """Build a markdown document for the 'no spec yet' case.

        Uses the `slug` + `state` passed in. Returns just the error text when
        no slug is set or the slug isn't in state (safety net for out-of-band
        callers).
        """
        if slug is None or state is None or slug not in state.tasks:
            return f"# {default_error}\n"

        task = state.tasks[slug]
        phase = task.phase
        branch = task.branch
        description = task.description or "_(no description)_"

        lines: list[str] = [
            f"# No spec yet for task `{slug}`",
            "",
            f"**Phase:** `{phase}`  ·  **Branch:** `{branch}`",
            "",
            "## Task description",
            description,
            "",
            "## Recent journal",
        ]

        entries = []
        if self._log_manager is not None:
            try:
                entries = self._log_manager.read(slug, last=10)
            except TypeError:
                entries = self._log_manager.read(slug)[-10:]

        if not entries:
            lines.append("_No journal entries yet._")
        else:
            for e in entries:
                ts = e.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"- **{ts}** — {e.message}")

        lines.append("")
        lines.append("_Write a spec with your preferred flow and save it to `docs/superpowers/specs/`._")
        return "\n".join(lines) + "\n"

    def rendered_text(self) -> str:
        """Test helper — returns last markdown source plus any error text."""
        return self._last_source + "\n" + self._last_error
```

Summary of diffs vs. before:
1. Constructor gains `state_manager`, `cli_task`, `cwd`. Keeps `state` as `self._initial_state` (backstop for existing unit tests that pre-load state).
2. New `_current_state()` helper — prefers `state_manager.load()` when provided.
3. `_refresh_content()` reads state via `_current_state()` each tick.
4. `_render_task_fallback` now takes `slug` + `state` explicitly instead of reading `self._task_filter` / `self._state`.

- [ ] **Step 4.2: Run the full spec-view test file**

Run: `pytest tests/cli/view/test_spec_view.py -v`
Expected: all existing tests still pass. The constructor is backwards-compatible (all new kwargs defaulted), and the fallback signature change is internal — the existing tests don't call `_render_task_fallback` directly.

If any test fails, fix it by passing the new arg explicitly, but the defaults should keep existing call sites green.

- [ ] **Step 4.3: Run full view test subdir**

Run: `pytest tests/cli/view/ -v`
Expected: all green.

- [ ] **Step 4.4: Commit**

```bash
git add src/mship/cli/view/spec.py
git commit -m "refactor(view): SpecView reads state per tick via state_manager"
mship journal "SpecView accepts state_manager + cli_task + cwd; fallback takes explicit slug+state" --action committed
```

---

## Task 5: `SpecView` — per-tick resolver tolerance

**Files:**
- Modify: `src/mship/cli/view/spec.py`
- Modify: `tests/cli/view/test_spec_view.py`

**Context:** Add resolver handling in `_refresh_content()` so watch mode renders a placeholder on resolver error. When `name_or_path is None`, resolve the task each tick (if a `cli_task` override was provided OR if `task_filter` is None, meaning the CLI didn't pre-resolve). Behavior matches `LogsView`.

- [ ] **Step 5.1: Write failing tests**

Append to `tests/cli/view/test_spec_view.py`:

```python
# --- watch-mode resolver tolerance ---

from dataclasses import dataclass as _dataclass, field as _field


@_dataclass
class _FakeSpecTask:
    slug: str
    description: str = ""
    phase: str = "plan"
    branch: str = "feat/x"
    worktrees: dict = _field(default_factory=dict)


class _FakeStateTasks:
    def __init__(self, tasks_dict):
        self.tasks = tasks_dict


class _MutableSpecStateMgr:
    def __init__(self, tasks_dict=None):
        self._tasks = tasks_dict or {}

    def set_tasks(self, tasks_dict):
        self._tasks = tasks_dict

    def load(self):
        return _FakeStateTasks(self._tasks)


@pytest.mark.asyncio
async def test_spec_view_watch_no_active_task_shows_placeholder(tmp_path):
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        state_manager=_MutableSpecStateMgr(tasks_dict={}),
        cli_task=None,
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No active task" in view.rendered_text()


@pytest.mark.asyncio
async def test_spec_view_watch_ambiguous_shows_placeholder(tmp_path):
    mgr = _MutableSpecStateMgr(tasks_dict={
        "alpha": _FakeSpecTask("alpha"),
        "beta":  _FakeSpecTask("beta"),
    })
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        state_manager=mgr,
        cli_task=None,
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "Multiple active tasks" in text
        assert "alpha" in text and "beta" in text


@pytest.mark.asyncio
async def test_spec_view_watch_unknown_slug_shows_placeholder(tmp_path):
    mgr = _MutableSpecStateMgr(tasks_dict={"other": _FakeSpecTask("other")})
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        state_manager=mgr,
        cli_task="missing-one",
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "missing-one" in view.rendered_text()


@pytest.mark.asyncio
async def test_spec_view_watch_transitions_to_fallback_when_task_appears(tmp_path):
    mgr = _MutableSpecStateMgr(tasks_dict={})
    view = SpecView(
        workspace_root=tmp_path,
        name_or_path=None,
        state_manager=mgr,
        cli_task=None,
        cwd=tmp_path,
        watch=True,
        interval=0.5,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No active task" in view.rendered_text()
        mgr.set_tasks({
            "solo": _FakeSpecTask(
                "solo",
                description="My task description.",
                phase="plan",
                branch="feat/solo",
            ),
        })
        view._refresh_content()
        await pilot.pause()
        text = view.rendered_text()
        assert "No active task" not in text
        # Either a spec was rendered (none in tmp_path/docs/superpowers/specs)
        # or the task fallback with the description appears.
        assert "My task description" in text or "No spec yet for task" in text
```

- [ ] **Step 5.2: Run tests to verify they fail**

Run: `pytest tests/cli/view/test_spec_view.py -v`
Expected: 4 FAILs — `_refresh_content()` does not yet render the placeholder on resolver errors.

- [ ] **Step 5.3: Add resolver handling to `_refresh_content`**

Edit `src/mship/cli/view/spec.py`:

At the top of the file, add imports next to the existing ones:

```python
import os
from mship.cli.view._placeholders import placeholder_for
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
    resolve_task,
)
```

Add a new method on `SpecView` immediately after `_current_state`:

```python
    def _resolve_task_slug(self, state) -> Optional[str]:
        """Return the task slug to render for this tick, or raise a resolver
        error. Returns None when `name_or_path` is set (resolution skipped).
        """
        if self._name_or_path is not None:
            return None
        if self._task_filter is not None:
            return self._task_filter
        task = resolve_task(
            state,
            cli_task=self._cli_task,
            env_task=os.environ.get("MSHIP_TASK"),
            cwd=self._cwd,
        )
        return task.slug
```

Update `_refresh_content` to wrap the resolver call:

```python
    def _refresh_content(self) -> None:
        assert self._markdown is not None
        assert self._error_static is not None
        assert self._body is not None
        was_at_end = self._body.scroll_y >= (self._body.max_scroll_y - 1)
        prev_y = self._body.scroll_y

        state = self._current_state()

        try:
            slug_for_tick = self._resolve_task_slug(state)
        except (NoActiveTaskError, AmbiguousTaskError, UnknownTaskError) as err:
            text = placeholder_for(err)
            self._last_source = text
            self._last_error = ""
            self._markdown.update(text)
            self._error_static.update("")
            self.call_after_refresh(self._restore_scroll, prev_y, was_at_end)
            return

        try:
            path = find_spec(
                self._workspace_root,
                self._name_or_path,
                task=slug_for_tick,
                state=state,
            )
            source = path.read_text()
            self._last_source = source
            self._last_error = ""
            self._markdown.update(source)
            self._error_static.update("")
        except SpecNotFoundError as e:
            if self._name_or_path is None:
                body = self._render_task_fallback(slug_for_tick, state, default_error=str(e))
                self._last_source = body
                self._last_error = ""
                self._markdown.update(body)
                self._error_static.update("")
            else:
                error_msg = f"Spec not found: {e}"
                self._last_source = ""
                self._last_error = error_msg
                self._markdown.update("")
                self._error_static.update(error_msg)
        self.call_after_refresh(self._restore_scroll, prev_y, was_at_end)
```

- [ ] **Step 5.4: Run tests to verify they pass**

Run: `pytest tests/cli/view/test_spec_view.py -v`
Expected: all tests pass (4 new + existing).

- [ ] **Step 5.5: Run full view test subdir**

Run: `pytest tests/cli/view/ -v`
Expected: all green.

- [ ] **Step 5.6: Commit**

```bash
git add src/mship/cli/view/spec.py tests/cli/view/test_spec_view.py
git commit -m "feat(view): SpecView tolerates unresolved task in --watch"
mship journal "SpecView re-resolves each tick in watch mode; placeholder on resolver error" --action committed
```

---

## Task 6: CLI handler for `view spec` — branch on `--watch`

**Files:**
- Modify: `src/mship/cli/view/spec.py`
- Modify: `tests/cli/view/test_view_cli.py`

**Context:** The `spec` command still calls `resolve_or_exit` in the CLI entry path for the `name_or_path is None` case (lines 222–226 of the current file, pre-Task-5). Branch on `watch`: non-watch keeps the existing `resolve_or_exit` call; watch skips it and passes `cli_task=task` into the view. Also pass `state_manager` + `cwd` into the view constructor for both paths so the view can reload state per tick.

- [ ] **Step 6.1: Write failing tests**

Append to `tests/cli/view/test_view_cli.py`:

```python
def test_spec_non_watch_no_task_exits_1(empty_workspace):
    runner = CliRunner()
    result = runner.invoke(app, ["view", "spec"])
    assert result.exit_code == 1
    assert "no active task" in (result.output or "").lower()


def test_spec_watch_no_task_constructs_view_without_exit(empty_workspace, monkeypatch):
    from mship.cli.view import spec as spec_mod
    captured = {}
    monkeypatch.setattr(spec_mod.SpecView, "run", lambda self: captured.setdefault("view", self))
    runner = CliRunner()
    result = runner.invoke(app, ["view", "spec", "--watch"])
    assert result.exit_code == 0, result.output
    view = captured["view"]
    assert view._task_filter is None
    assert view._cli_task is None
    assert view._watch is True
    assert view._state_manager is not None


def test_spec_watch_with_unknown_task_constructs_view_without_exit(empty_workspace, monkeypatch):
    from mship.cli.view import spec as spec_mod
    captured = {}
    monkeypatch.setattr(spec_mod.SpecView, "run", lambda self: captured.setdefault("view", self))
    runner = CliRunner()
    result = runner.invoke(app, ["view", "spec", "--watch", "--task", "missing"])
    assert result.exit_code == 0, result.output
    view = captured["view"]
    assert view._task_filter is None
    assert view._cli_task == "missing"
```

- [ ] **Step 6.2: Run tests to verify they fail**

Run: `pytest tests/cli/view/test_view_cli.py -v`
Expected: `test_spec_watch_no_task_constructs_view_without_exit` and `test_spec_watch_with_unknown_task_constructs_view_without_exit` FAIL with `Exit 1` (resolver fired in CLI handler), and `test_spec_non_watch_no_task_exits_1` PASSes (existing behavior).

- [ ] **Step 6.3: Update `spec` CLI handler**

Replace the `register()` function in `src/mship/cli/view/spec.py` with:

```python
def register(app: typer.Typer, get_container):
    @app.command()
    def spec(
        name_or_path: Optional[str] = typer.Argument(None),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        web: bool = typer.Option(False, "--web", help="Serve rendered HTML on localhost"),
        port: Optional[int] = typer.Option(None, "--port", help="Explicit port for --web"),
        task: Optional[str] = typer.Option(None, "--task", help="Narrow to one task's worktrees"),
    ):
        """Render a spec file (newest by default)."""
        from pathlib import Path as _P
        if task is not None and name_or_path is not None:
            typer.echo("Error: --task and an explicit spec name are mutually exclusive.", err=True)
            raise typer.Exit(code=1)

        container = get_container()
        workspace_root = _P(container.config_path()).parent
        state = container.state_manager().load()

        # Resolve target task. If the user specified an explicit spec name,
        # skip task resolution entirely (rendering is name-driven). If --watch
        # is set, defer task resolution into the view so resolver errors
        # become placeholder text instead of exit-1.
        resolved_task_slug: Optional[str] = None
        cli_task_for_view: Optional[str] = None
        if name_or_path is None:
            if watch:
                cli_task_for_view = task
            else:
                from mship.cli._resolve import resolve_or_exit
                t = resolve_or_exit(state, task)
                resolved_task_slug = t.slug

        # --web still requires a resolvable spec path at request time.
        if web:
            try:
                path = find_spec(
                    workspace_root, name_or_path, task=resolved_task_slug, state=state,
                )
            except SpecNotFoundError as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(code=1)
            _serve_web(path, port)
            return

        view = SpecView(
            workspace_root=workspace_root,
            name_or_path=name_or_path,
            task=resolved_task_slug,
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            cli_task=cli_task_for_view,
            cwd=_P.cwd(),
            watch=watch,
            interval=interval,
        )
        view.run()
```

Key changes vs. before:
1. When `name_or_path is None`: branch on `watch`. Watch skips `resolve_or_exit` and sets `cli_task_for_view = task`.
2. Constructor now receives `state_manager` (not pre-loaded `state`), `cli_task`, `cwd`.
3. `--web` path unchanged: still calls `find_spec` eagerly and exits 1 on `SpecNotFoundError` (we didn't scope `--web` in the spec).

- [ ] **Step 6.4: Run tests to verify they pass**

Run: `pytest tests/cli/view/test_view_cli.py -v`
Expected: all 6 tests pass.

- [ ] **Step 6.5: Run full test suite**

Run: `pytest tests/ -v --ignore=tests/core/view/test_web_port.py`
Expected: green. (The `test_web_port.py` ignore is because of pre-existing port-binding flakes on some machines; unrelated to this change.)

- [ ] **Step 6.6: Commit**

```bash
git add src/mship/cli/view/spec.py tests/cli/view/test_view_cli.py
git commit -m "feat(view): spec CLI defers task resolution in --watch"
mship journal "view spec CLI branches on --watch; cli-level regression tests green" --action committed
```

---

## Task 7: Manual smoke + finish PR

**Files:**
- None (verification only)

**Context:** Confirm end-to-end behavior matches the success criterion in the spec. The full test suite is already green from Task 6; this task verifies the human-observable behavior.

- [ ] **Step 7.1: Reinstall the tool and start from an empty workspace**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/view-journal-and-spec-survive-no-task-in-watch-mode
uv tool install --reinstall --from . mothership
# Use an empty scratch workspace to guarantee 0 tasks.
rm -rf /tmp/view-watch-smoke
mkdir -p /tmp/view-watch-smoke
cd /tmp/view-watch-smoke
cat > mothership.yaml <<'EOF'
workspace: view-watch-smoke
repos: {}
EOF
mkdir -p .mothership
```

- [ ] **Step 7.2: Non-watch contract still exits 1**

```bash
mship view journal; echo "EXIT: $?"
mship view spec;    echo "EXIT: $?"
```

Expected: both print `ERROR: no active task; run \`mship spawn "description"\` to start one` and `EXIT: 1`.

- [ ] **Step 7.3: Watch mode mounts with placeholder (journal)**

In one terminal:

```bash
cd /tmp/view-watch-smoke
mship view journal --watch
```

Expected: the TUI opens. Top-left shows `No active task. Run \`mship spawn "description"\` to start one.`. Pane stays open, does not exit 1. Press `q` to close.

- [ ] **Step 7.4: Watch mode mounts with placeholder (spec)**

Same thing for spec:

```bash
cd /tmp/view-watch-smoke
mship view spec --watch
```

Expected: same placeholder text in the spec pane. Press `q` to close.

- [ ] **Step 7.5: Cleanup**

```bash
rm -rf /tmp/view-watch-smoke
```

- [ ] **Step 7.6: Full test suite final check**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/view-journal-and-spec-survive-no-task-in-watch-mode
pytest tests/ -v --ignore=tests/core/view/test_web_port.py
```

Expected: green.

- [ ] **Step 7.7: Finish — open PR**

```bash
cat > /tmp/view-watch-body.md <<'EOF'
## Summary

`mship view journal --watch` and `mship view spec --watch` no longer exit 1 when the workspace has no resolvable task. Instead the pane mounts, shows a short placeholder, and re-resolves the task every tick — so as soon as a task becomes resolvable the pane picks it up without a restart.

Non-watch behavior is unchanged: both commands still exit 1 on resolver failure (regression guarded at the CLI runner level).

## Scope

- `view journal --watch`: re-resolves per tick; placeholder on `NoActiveTask` / `Ambiguous` / `Unknown`.
- `view spec --watch`: same, for the `name_or_path is None` path. Explicit spec names are unaffected.
- `view status` and `view diff` already handle empty state gracefully — untouched.
- `--web` path unchanged.

## New file

- `src/mship/cli/view/_placeholders.py` — single source of placeholder wording for all three resolver errors. Used by both views; asserted against directly in tests.

## Test plan

- [x] `tests/cli/view/test_placeholders.py`: 4 unit tests for the helper (all three resolver errors + unknown-exception re-raise).
- [x] `tests/cli/view/test_logs_view.py`: watch-mode placeholder for each resolver error + transition + non-watch resolver-not-called regression.
- [x] `tests/cli/view/test_spec_view.py`: same four cases for `SpecView`.
- [x] `tests/cli/view/test_view_cli.py`: CliRunner-level tests asserting non-watch exits 1 and watch mounts without exit 1 (for both `journal` and `spec`).
- [x] Manual smoke in a scratch empty workspace: journal pane + spec pane both display the "No active task" placeholder, do not exit 1.

Closes the reporter's issue: journal and specs view exit 1 when there is no task even in --watch mode.
EOF

mship finish --body-file /tmp/view-watch-body.md
```

Expected: PR URL returned. Done.

---

## Done when

- [x] `placeholder_for()` maps all three resolver errors; unit-tested.
- [x] `LogsView` re-resolves each tick in watch mode; placeholder on resolver error; non-watch unchanged.
- [x] `SpecView` re-resolves each tick in watch mode; placeholder on resolver error; explicit-name path unchanged; `--web` path unchanged.
- [x] Non-watch CLI contract preserved: `mship view journal` / `mship view spec` with no task exit 1 with stderr error.
- [x] Watch CLI tolerance: `mship view journal --watch` / `mship view spec --watch` with no task mount without exit.
- [x] Manual smoke confirms placeholder text and pane persistence.
- [x] Full pytest green (excluding pre-existing `test_web_port.py` port-bind flakes).
