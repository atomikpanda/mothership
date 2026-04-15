# `mship view` God View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every `mship view` subcommand default to an all-tasks "God view" that spans every task's worktrees, with `--task <slug>` to narrow to a single task.

**Architecture:** Add a pure data layer (`core/view/task_index.py`) that summarizes every task in state and scans each task's worktrees for specs. Add a shared `TaskPicker` widget and wire each existing `view` subcommand to open the picker by default and accept `--task`. Spec resolution gains a `task=` kwarg for per-worktree search.

**Tech Stack:** Python 3.14, Pydantic, Typer, Textual TUI, pytest.

**Spec:** `docs/superpowers/specs/2026-04-15-view-god-view-design.md`

---

## File Structure

**New:**
- `src/mship/core/view/task_index.py` — `TaskSummary`, `build_task_index`, `SpecEntry`, `find_all_specs`
- `src/mship/cli/view/_picker.py` — shared `TaskPicker` Textual widget
- `tests/core/view/test_task_index.py` — unit tests for data layer

**Modified:**
- `src/mship/core/view/spec_discovery.py` — add `task=` kwarg to `find_spec`
- `src/mship/cli/view/status.py` — default stacked God view, `--task` narrows
- `src/mship/cli/view/spec.py` — default picker, `--task` narrows, mutex with `name_or_path`
- `src/mship/cli/view/logs.py` — default picker, promote to `--task` flag (rename positional)
- `src/mship/cli/view/diff.py` — default picker, `--task` narrows
- `tests/core/view/test_spec_discovery.py` — extend with `task=` cases
- `tests/cli/view/test_*.py` — new picker/narrow cases in each

---

## Task 1: Data layer — `TaskSummary` + `build_task_index`

**Files:**
- Create: `src/mship/core/view/task_index.py`
- Test: `tests/core/view/test_task_index.py`

- [ ] **Step 1.1: Write the failing test**

Create `tests/core/view/test_task_index.py`:

```python
from datetime import datetime, timezone, timedelta
from pathlib import Path

from mship.core.state import Task, WorkspaceState, TestResult
from mship.core.view.task_index import TaskSummary, build_task_index


def _task(slug: str, **over) -> Task:
    base = dict(
        slug=slug,
        description=slug,
        phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"],
        worktrees={},
        branch=f"feat/{slug}",
    )
    base.update(over)
    return Task(**base)


def test_build_task_index_empty(tmp_path: Path):
    state = WorkspaceState()
    assert build_task_index(state, tmp_path) == []


def test_build_task_index_summarizes_active_task(tmp_path: Path):
    wt = tmp_path / "wt-a"
    wt.mkdir()
    (wt / "docs" / "superpowers" / "specs").mkdir(parents=True)
    (wt / "docs" / "superpowers" / "specs" / "s.md").write_text("# s")
    t = _task("a", worktrees={"mothership": wt})
    state = WorkspaceState(tasks={"a": t})

    [summary] = build_task_index(state, tmp_path)
    assert isinstance(summary, TaskSummary)
    assert summary.slug == "a"
    assert summary.phase == "dev"
    assert summary.affected_repos == ["mothership"]
    assert summary.worktrees == {"mothership": wt}
    assert summary.finished_at is None
    assert summary.spec_count == 1
    assert summary.orphan is False
    assert summary.tests_failing is False


def test_build_task_index_flags_orphan_worktree(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    t = _task("a", worktrees={"mothership": missing})
    state = WorkspaceState(tasks={"a": t})
    [summary] = build_task_index(state, tmp_path)
    assert summary.orphan is True


def test_build_task_index_flags_tests_failing(tmp_path: Path):
    t = _task("a", test_results={"mothership": TestResult(status="fail", at=datetime.now(timezone.utc))})
    state = WorkspaceState(tasks={"a": t})
    [summary] = build_task_index(state, tmp_path)
    assert summary.tests_failing is True


def test_build_task_index_orders_active_before_finished(tmp_path: Path):
    now = datetime.now(timezone.utc)
    active = _task("active", created_at=now - timedelta(hours=1))
    finished = _task("finished", created_at=now - timedelta(hours=2), finished_at=now)
    state = WorkspaceState(tasks={"finished": finished, "active": active})
    slugs = [s.slug for s in build_task_index(state, tmp_path)]
    assert slugs == ["active", "finished"]


def test_build_task_index_orders_active_by_created_desc(tmp_path: Path):
    now = datetime.now(timezone.utc)
    older = _task("older", created_at=now - timedelta(hours=2))
    newer = _task("newer", created_at=now - timedelta(minutes=5))
    state = WorkspaceState(tasks={"older": older, "newer": newer})
    assert [s.slug for s in build_task_index(state, tmp_path)] == ["newer", "older"]
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `uv run pytest tests/core/view/test_task_index.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mship.core.view.task_index'`.

- [ ] **Step 1.3: Create the module with minimal implementation**

Create `src/mship/core/view/task_index.py`:

```python
"""Task-index data layer for the cross-task `mship view` God view."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mship.core.state import Task, WorkspaceState


@dataclass(frozen=True)
class TaskSummary:
    slug: str
    phase: str
    branch: str
    affected_repos: list[str]
    worktrees: dict[str, Path]
    finished_at: datetime | None
    blocked_reason: str | None
    created_at: datetime
    spec_count: int
    orphan: bool
    tests_failing: bool


def _summarize(task: Task) -> TaskSummary:
    worktrees = {repo: Path(p) for repo, p in task.worktrees.items()}
    orphan = any(not p.exists() for p in worktrees.values())
    spec_count = 0
    for p in worktrees.values():
        specs_dir = p / "docs" / "superpowers" / "specs"
        if specs_dir.is_dir():
            spec_count += sum(1 for f in specs_dir.iterdir() if f.is_file() and f.suffix == ".md")
    tests_failing = any(r.status == "fail" for r in task.test_results.values())
    return TaskSummary(
        slug=task.slug,
        phase=task.phase,
        branch=task.branch,
        affected_repos=list(task.affected_repos),
        worktrees=worktrees,
        finished_at=task.finished_at,
        blocked_reason=task.blocked_reason,
        created_at=task.created_at,
        spec_count=spec_count,
        orphan=orphan,
        tests_failing=tests_failing,
    )


def build_task_index(state: WorkspaceState, workspace_root: Path) -> list[TaskSummary]:
    """Active tasks first (by created_at desc), then finished-awaiting-close (also desc)."""
    summaries = [_summarize(t) for t in state.tasks.values()]
    active = sorted(
        [s for s in summaries if s.finished_at is None],
        key=lambda s: s.created_at, reverse=True,
    )
    finished = sorted(
        [s for s in summaries if s.finished_at is not None],
        key=lambda s: s.created_at, reverse=True,
    )
    return active + finished
```

- [ ] **Step 1.4: Run tests to verify pass**

Run: `uv run pytest tests/core/view/test_task_index.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 1.5: Commit**

```bash
git add src/mship/core/view/task_index.py tests/core/view/test_task_index.py
git commit -m "feat(view): add task-index data layer for God view"
```

---

## Task 2: Spec discovery across worktrees

**Files:**
- Modify: `src/mship/core/view/task_index.py` — append `SpecEntry` + `find_all_specs`
- Modify: `src/mship/core/view/spec_discovery.py` — add `task=` kwarg to `find_spec`
- Test: `tests/core/view/test_task_index.py` (extend)
- Test: `tests/core/view/test_spec_discovery.py` (extend)

- [ ] **Step 2.1: Write failing tests for `find_all_specs`**

Append to `tests/core/view/test_task_index.py`:

```python
from mship.core.view.task_index import SpecEntry, find_all_specs


def _write_spec(path: Path, body: str = "# Title\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_find_all_specs_scans_each_worktree(tmp_path: Path):
    wt_a = tmp_path / "wt-a"
    wt_b = tmp_path / "wt-b"
    _write_spec(wt_a / "docs" / "superpowers" / "specs" / "a.md", "# Alpha\n")
    _write_spec(wt_b / "docs" / "superpowers" / "specs" / "b.md", "# Beta\n")
    state = WorkspaceState(tasks={
        "a": _task("a", worktrees={"mothership": wt_a}),
        "b": _task("b", worktrees={"mothership": wt_b}),
    })
    specs = find_all_specs(state, tmp_path)
    titles = {(s.task_slug, s.path.name, s.title) for s in specs}
    assert ("a", "a.md", "Alpha") in titles
    assert ("b", "b.md", "Beta") in titles


def test_find_all_specs_includes_main_checkout_with_none_slug(tmp_path: Path):
    _write_spec(tmp_path / "docs" / "superpowers" / "specs" / "legacy.md", "# Legacy\n")
    state = WorkspaceState()
    specs = find_all_specs(state, tmp_path)
    assert [(s.task_slug, s.path.name) for s in specs] == [(None, "legacy.md")]


def test_find_all_specs_title_falls_back_to_stem(tmp_path: Path):
    _write_spec(tmp_path / "docs" / "superpowers" / "specs" / "untitled.md", "no heading here\n")
    [entry] = find_all_specs(WorkspaceState(), tmp_path)
    assert entry.title == "untitled"


def test_find_all_specs_empty(tmp_path: Path):
    assert find_all_specs(WorkspaceState(), tmp_path) == []
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `uv run pytest tests/core/view/test_task_index.py::test_find_all_specs_scans_each_worktree -v`
Expected: FAIL with `ImportError: cannot import name 'SpecEntry' from 'mship.core.view.task_index'`.

- [ ] **Step 2.3: Implement `SpecEntry` + `find_all_specs`**

Append to `src/mship/core/view/task_index.py`:

```python
@dataclass(frozen=True)
class SpecEntry:
    task_slug: str | None           # None == main checkout (legacy)
    path: Path
    mtime: float
    title: str


_SPEC_SUBDIR = Path("docs") / "superpowers" / "specs"


def _extract_title(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as fh:
            head = fh.read(2048)
    except OSError:
        return path.stem
    for line in head.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or path.stem
    return path.stem


def _scan_specs_dir(specs_dir: Path, task_slug: str | None) -> list[SpecEntry]:
    if not specs_dir.is_dir():
        return []
    out: list[SpecEntry] = []
    for f in specs_dir.iterdir():
        if not f.is_file() or f.suffix != ".md":
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        out.append(SpecEntry(task_slug=task_slug, path=f, mtime=mtime, title=_extract_title(f)))
    return out


def find_all_specs(state: WorkspaceState, workspace_root: Path) -> list[SpecEntry]:
    """All specs across every task's worktrees + the main checkout.

    Grouped by task (active first, finished last, None/main first within the
    None group); within each group, newest mtime first.
    """
    index = build_task_index(state, workspace_root)
    entries: list[SpecEntry] = []
    seen_paths: set[Path] = set()

    # Main-checkout specs get task_slug=None.
    main_entries = sorted(
        _scan_specs_dir(workspace_root / _SPEC_SUBDIR, None),
        key=lambda e: e.mtime, reverse=True,
    )
    for e in main_entries:
        if e.path not in seen_paths:
            entries.append(e)
            seen_paths.add(e.path)

    for summary in index:
        per_task: list[SpecEntry] = []
        for wt in summary.worktrees.values():
            per_task.extend(_scan_specs_dir(wt / _SPEC_SUBDIR, summary.slug))
        per_task.sort(key=lambda e: e.mtime, reverse=True)
        for e in per_task:
            if e.path not in seen_paths:
                entries.append(e)
                seen_paths.add(e.path)
    return entries
```

- [ ] **Step 2.4: Run task_index tests**

Run: `uv run pytest tests/core/view/test_task_index.py -v`
Expected: all 10 tests PASS.

- [ ] **Step 2.5: Write failing test for `find_spec(task=...)`**

Append to `tests/core/view/test_spec_discovery.py`:

```python
from mship.core.state import WorkspaceState, Task
from datetime import datetime, timezone


def _make_task(slug: str, worktree: Path) -> Task:
    return Task(
        slug=slug, description=slug, phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"],
        worktrees={"mothership": worktree},
        branch=f"feat/{slug}",
    )


def test_find_spec_with_task_returns_newest_in_task_worktree(tmp_path: Path):
    wt = tmp_path / "wt-a"
    specs = wt / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    _touch(specs / "old.md", time.time() - 100)
    _touch(specs / "new.md", time.time() - 5)
    state = WorkspaceState(tasks={"a": _make_task("a", wt)})

    result = find_spec(workspace_root=tmp_path, name_or_path=None, task="a", state=state)
    assert result.name == "new.md"


def test_find_spec_unknown_task_raises(tmp_path: Path):
    state = WorkspaceState()
    with pytest.raises(SpecNotFoundError):
        find_spec(workspace_root=tmp_path, name_or_path=None, task="nope", state=state)


def test_find_spec_by_name_searches_all_worktrees_when_no_task(tmp_path: Path):
    wt = tmp_path / "wt-a"
    specs = wt / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "found.md").write_text("# found\n")
    state = WorkspaceState(tasks={"a": _make_task("a", wt)})

    result = find_spec(workspace_root=tmp_path, name_or_path="found", state=state)
    assert result == specs / "found.md"
```

- [ ] **Step 2.6: Run spec_discovery tests to see failures**

Run: `uv run pytest tests/core/view/test_spec_discovery.py -v`
Expected: 3 new tests FAIL with `TypeError: find_spec() got an unexpected keyword argument 'task'` or similar.

- [ ] **Step 2.7: Update `find_spec` signature**

Replace `src/mship/core/view/spec_discovery.py` entirely with:

```python
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from mship.core.state import WorkspaceState


class SpecNotFoundError(Exception):
    pass


SPEC_SUBDIR = Path("docs") / "superpowers" / "specs"


def find_spec(
    workspace_root: Path,
    name_or_path: str | None,
    *,
    task: str | None = None,
    state: Optional["WorkspaceState"] = None,
) -> Path:
    """Resolve a spec file.

    - `name_or_path=None, task=None`: newest in `workspace_root` specs dir.
    - `name_or_path=None, task=<slug>`: newest across that task's worktrees.
    - `name_or_path=<name>, task=<slug>`: that name, searching only the task's worktrees.
    - `name_or_path=<name>, task=None, state=<state>`: that name, searching main + every worktree.
    - `name_or_path=<absolute path>`: that literal file.
    """
    if name_or_path is not None:
        candidate = Path(name_or_path)
        if candidate.is_absolute():
            if candidate.is_file():
                return candidate
            raise SpecNotFoundError(f"Spec not found: {name_or_path}")

    search_roots = _resolve_search_roots(workspace_root, task, state)

    if name_or_path is None:
        return _newest_across(search_roots, task)

    for root in search_roots:
        for candidate_name in (name_or_path, f"{name_or_path}.md"):
            p = root / candidate_name
            if p.is_file():
                return p

    available_msg = _available_msg(search_roots)
    where = f"task {task!r}" if task else "any known location"
    raise SpecNotFoundError(f"Spec not found: {name_or_path!r} (searched {where}).{available_msg}")


def _resolve_search_roots(
    workspace_root: Path,
    task: str | None,
    state: "WorkspaceState | None",
) -> list[Path]:
    if task is not None:
        if state is None or task not in state.tasks:
            raise SpecNotFoundError(f"Unknown task: {task!r}")
        worktrees = state.tasks[task].worktrees
        roots = [Path(p) / SPEC_SUBDIR for p in worktrees.values()]
        return [r for r in roots if r.is_dir()] or roots
    roots: list[Path] = [workspace_root / SPEC_SUBDIR]
    if state is not None:
        for t in state.tasks.values():
            for wt in t.worktrees.values():
                roots.append(Path(wt) / SPEC_SUBDIR)
    return roots


def _newest_across(roots: list[Path], task: str | None) -> Path:
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(p for p in root.iterdir() if p.is_file() and p.suffix == ".md")
    if not candidates:
        where = f"task {task!r}" if task else f"{roots[0] if roots else '?'}"
        raise SpecNotFoundError(f"No specs found in {where}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _available_msg(roots: list[Path]) -> str:
    names: set[str] = set()
    for r in roots:
        if r.is_dir():
            names.update(p.name for p in r.iterdir() if p.is_file() and p.suffix == ".md")
    if not names:
        return ""
    shown = sorted(names)[:5]
    rest = len(names) - len(shown)
    suffix = f" ({rest} more)" if rest > 0 else ""
    return f" Available: {', '.join(shown)}{suffix}."
```

- [ ] **Step 2.8: Run full spec_discovery + task_index suites**

Run: `uv run pytest tests/core/view/test_spec_discovery.py tests/core/view/test_task_index.py -v`
Expected: all PASS.

- [ ] **Step 2.9: Commit**

```bash
git add src/mship/core/view/task_index.py src/mship/core/view/spec_discovery.py tests/core/view/test_task_index.py tests/core/view/test_spec_discovery.py
git commit -m "feat(view): spec discovery across task worktrees"
```

---

## Task 3: Shared `TaskPicker` widget

**Files:**
- Create: `src/mship/cli/view/_picker.py`
- Test: `tests/cli/view/test_picker.py`

- [ ] **Step 3.1: Write failing test**

Create `tests/cli/view/test_picker.py`:

```python
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from mship.core.state import Task, WorkspaceState, TestResult
from mship.core.view.task_index import build_task_index
from mship.cli.view._picker import TaskPicker, picker_rows


def _t(slug: str, **over) -> Task:
    base = dict(
        slug=slug, description=slug, phase="dev",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        affected_repos=["mothership"], worktrees={}, branch=f"feat/{slug}",
    )
    base.update(over)
    return Task(**base)


def test_picker_rows_contain_slug_phase_flags(tmp_path: Path):
    now = datetime.now(timezone.utc)
    state = WorkspaceState(tasks={
        "a": _t("a"),
        "b": _t("b", finished_at=now),
        "c": _t("c", blocked_reason="waiting on review"),
        "d": _t("d", test_results={"mothership": TestResult(status="fail", at=now)}),
    })
    index = build_task_index(state, tmp_path)
    rows = picker_rows(index)
    slugs = {r.slug: r for r in rows}
    assert "⚠ close" in slugs["b"].flags
    assert "🚫 blocked" in slugs["c"].flags
    assert "🧪 fail" in slugs["d"].flags
    assert slugs["a"].flags == ""


@pytest.mark.asyncio
async def test_picker_renders_all_rows(tmp_path: Path):
    state = WorkspaceState(tasks={"a": _t("a"), "b": _t("b")})
    index = build_task_index(state, tmp_path)
    app = TaskPicker(rows=picker_rows(index), extra_columns=())
    async with app.run_test() as pilot:
        await pilot.pause()
        labels = app.row_slugs()
        assert labels == ["a", "b"] or labels == ["b", "a"]
```

- [ ] **Step 3.2: Run test to verify it fails**

Run: `uv run pytest tests/cli/view/test_picker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mship.cli.view._picker'`.

- [ ] **Step 3.3: Implement the widget**

Create `src/mship/cli/view/_picker.py`:

```python
"""Shared TaskPicker: cross-task selection widget for view commands."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Static

from mship.cli.view._base import ViewApp
from mship.core.view.task_index import TaskSummary


@dataclass(frozen=True)
class PickerRow:
    slug: str
    phase: str
    repos: str
    age: str
    flags: str
    extras: tuple[str, ...] = ()


def _age(created_at: datetime) -> str:
    delta = datetime.now(timezone.utc) - created_at
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _flags_for(summary: TaskSummary) -> str:
    parts: list[str] = []
    if summary.finished_at is not None:
        parts.append("⚠ close")
    if summary.blocked_reason:
        parts.append("🚫 blocked")
    if summary.tests_failing:
        parts.append("🧪 fail")
    if summary.orphan:
        parts.append("⚠ orphan")
    return " ".join(parts)


def picker_rows(
    index: Sequence[TaskSummary],
    extra: Callable[[TaskSummary], tuple[str, ...]] | None = None,
) -> list[PickerRow]:
    return [
        PickerRow(
            slug=s.slug,
            phase=s.phase,
            repos=",".join(s.affected_repos),
            age=_age(s.created_at),
            flags=_flags_for(s),
            extras=extra(s) if extra else (),
        )
        for s in index
    ]


class TaskPicker(ViewApp):
    """All-tasks picker. Subclasses (or callers) pass rows + an on_select callback."""

    BINDINGS = ViewApp.BINDINGS + [
        Binding("enter", "select_cursor", "Open", show=True),
    ]

    def __init__(
        self,
        rows: Sequence[PickerRow],
        extra_columns: Sequence[str] = (),
        on_select: Callable[[str], None] | None = None,
        **kw,
    ):
        super().__init__(**kw)
        self._rows = list(rows)
        self._extra_columns = tuple(extra_columns)
        self._on_select = on_select
        self._table: DataTable | None = None
        self._empty: Static | None = None

    def compose(self) -> ComposeResult:
        if not self._rows:
            self._empty = Static(
                "No tasks. Run `mship spawn \"…\"` to start one.", expand=True,
            )
            yield self._empty
            return
        self._table = DataTable(cursor_type="row")
        self._table.add_columns("slug", "phase", "repos", "age", "flags", *self._extra_columns)
        for r in self._rows:
            self._table.add_row(r.slug, r.phase, r.repos, r.age, r.flags, *r.extras, key=r.slug)
        yield self._table

    def on_mount(self) -> None:
        if self._table is not None:
            self._table.focus()

    def action_select_cursor(self) -> None:
        if self._table is None or self._on_select is None:
            return
        if self._table.cursor_row is None:
            return
        slug = self._rows[self._table.cursor_row].slug
        self._on_select(slug)

    # Test helpers
    def row_slugs(self) -> list[str]:
        return [r.slug for r in self._rows]
```

- [ ] **Step 3.4: Run tests**

Run: `uv run pytest tests/cli/view/test_picker.py -v`
Expected: all PASS.

- [ ] **Step 3.5: Commit**

```bash
git add src/mship/cli/view/_picker.py tests/cli/view/test_picker.py
git commit -m "feat(view): shared TaskPicker widget"
```

---

## Task 4: Rewire `view status` — stacked God view + `--task`

**Files:**
- Modify: `src/mship/cli/view/status.py`
- Test: `tests/cli/view/test_status_view.py`

- [ ] **Step 4.1: Write failing test**

Append to `tests/cli/view/test_status_view.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.core.state import Task, WorkspaceState


def _task(slug: str, **over) -> Task:
    base = dict(
        slug=slug, description=slug, phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"], worktrees={}, branch=f"feat/{slug}",
    )
    base.update(over)
    return Task(**base)


@pytest.mark.asyncio
async def test_status_view_stacks_all_tasks(monkeypatch, tmp_path: Path):
    from mship.cli.view.status import StatusView

    class FakeSM:
        def load(self):
            return WorkspaceState(tasks={"a": _task("a"), "b": _task("b")}, current_task=None)

    view = StatusView(state_manager=FakeSM(), workspace_root=tmp_path, task_filter=None)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
    assert "Task:   a" in text
    assert "Task:   b" in text


def test_status_cli_rejects_unknown_task():
    runner = CliRunner()
    result = runner.invoke(app, ["view", "status", "--task", "does-not-exist"])
    assert result.exit_code != 0
    assert "does-not-exist" in result.output
```

- [ ] **Step 4.2: Run test to verify failures**

Run: `uv run pytest tests/cli/view/test_status_view.py::test_status_view_stacks_all_tasks tests/cli/view/test_status_view.py::test_status_cli_rejects_unknown_task -v`
Expected: FAIL — `StatusView` missing `workspace_root`/`task_filter` kwargs; CLI has no `--task` flag.

- [ ] **Step 4.3: Rewrite `status.py`**

Replace `src/mship/cli/view/status.py`:

```python
from pathlib import Path
from typing import Optional

import typer

from mship.cli.view._base import ViewApp
from mship.core.state import Task, WorkspaceState
from mship.core.view.task_index import build_task_index


def _render_task(task: Task) -> str:
    from mship.util.duration import format_relative

    lines = [f"Task:   {task.slug}"]
    if task.finished_at is not None:
        lines.append(
            f"⚠ Finished: {format_relative(task.finished_at)} — run `mship close` after merge"
        )
    if getattr(task, "active_repo", None) is not None:
        lines.append(f"Active repo: {task.active_repo}")
    phase_line = task.phase
    if task.phase_entered_at is not None:
        phase_line = f"{task.phase} (entered {format_relative(task.phase_entered_at)})"
    if task.blocked_reason:
        phase_line += f"  (BLOCKED: {task.blocked_reason})"
    lines.append(f"Phase:  {phase_line}")
    lines.append(f"Branch: {task.branch}")
    lines.append(f"Repos:  {', '.join(task.affected_repos)}")
    if task.worktrees:
        lines.append("Worktrees:")
        for repo, path in task.worktrees.items():
            lines.append(f"  {repo}: {path}")
    if task.test_results:
        lines.append("Tests:")
        for repo, result in task.test_results.items():
            lines.append(f"  {repo}: {result.status}")
    return "\n".join(lines)


class StatusView(ViewApp):
    def __init__(self, state_manager, workspace_root: Path, task_filter: Optional[str], **kw):
        super().__init__(**kw)
        self._state_manager = state_manager
        self._workspace_root = workspace_root
        self._task_filter = task_filter

    def gather(self) -> str:
        state: WorkspaceState = self._state_manager.load()
        if self._task_filter is not None:
            task = state.tasks.get(self._task_filter)
            if task is None:
                return f"Unknown task: {self._task_filter}"
            return _render_task(task)
        index = build_task_index(state, self._workspace_root)
        if not index:
            return "No tasks. Run `mship spawn \"…\"` to start one."
        blocks = [_render_task(state.tasks[s.slug]) for s in index]
        return "\n\n─────────────\n\n".join(blocks)


def register(app: typer.Typer, get_container):
    @app.command()
    def status(
        task: Optional[str] = typer.Option(None, "--task", help="Narrow to one task slug"),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
    ):
        """Live workspace status view (all tasks by default)."""
        from pathlib import Path as _P
        container = get_container()
        if task is not None:
            state = container.state_manager().load()
            if task not in state.tasks:
                known = ", ".join(sorted(state.tasks.keys())) or "(none)"
                typer.echo(f"Unknown task '{task}'. Known: {known}.", err=True)
                raise typer.Exit(code=1)
        workspace_root = _P(container.config_path()).parent
        view = StatusView(
            state_manager=container.state_manager(),
            workspace_root=workspace_root,
            task_filter=task,
            watch=watch,
            interval=interval,
        )
        view.run()
```

- [ ] **Step 4.4: Fix any pre-existing status tests**

Run: `uv run pytest tests/cli/view/test_status_view.py -v`
Expected: any old test that instantiated `StatusView(state_manager=...)` without `workspace_root`/`task_filter` FAILS.

For each failing old test, update the `StatusView(...)` construction to pass `workspace_root=tmp_path, task_filter=None`. No behavior change.

- [ ] **Step 4.5: Run all status tests**

Run: `uv run pytest tests/cli/view/test_status_view.py -v`
Expected: all PASS.

- [ ] **Step 4.6: Commit**

```bash
git add src/mship/cli/view/status.py tests/cli/view/test_status_view.py
git commit -m "feat(view): stacked all-tasks status + --task narrow"
```

---

## Task 5: Rewire `view spec` — index picker + `--task`

**Files:**
- Modify: `src/mship/cli/view/spec.py`
- Test: `tests/cli/view/test_spec_view.py`

- [ ] **Step 5.1: Write failing test**

Append to `tests/cli/view/test_spec_view.py`:

```python
from pathlib import Path
from typer.testing import CliRunner

from mship.cli import app


def test_spec_cli_rejects_task_with_name():
    runner = CliRunner()
    result = runner.invoke(app, ["view", "spec", "--task", "a", "some-name"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output.lower() or "--task" in result.output


def test_spec_cli_rejects_unknown_task(tmp_path, monkeypatch):
    runner = CliRunner()
    result = runner.invoke(app, ["view", "spec", "--task", "nope"])
    assert result.exit_code != 0
    assert "nope" in result.output
```

- [ ] **Step 5.2: Run test**

Run: `uv run pytest tests/cli/view/test_spec_view.py::test_spec_cli_rejects_task_with_name tests/cli/view/test_spec_view.py::test_spec_cli_rejects_unknown_task -v`
Expected: FAIL — `--task` flag doesn't exist.

- [ ] **Step 5.3: Add the spec-index widget + `--task` flag**

Edit `src/mship/cli/view/spec.py`. Inside the `register()` `spec()` function, replace the body with:

```python
    from pathlib import Path as _P
    container = get_container()
    workspace_root = _P(container.config_path()).parent
    state = container.state_manager().load()

    if task is not None and name_or_path is not None:
        typer.echo("Error: --task and an explicit spec name are mutually exclusive.", err=True)
        raise typer.Exit(code=1)
    if task is not None and task not in state.tasks:
        known = ", ".join(sorted(state.tasks.keys())) or "(none)"
        typer.echo(f"Unknown task '{task}'. Known: {known}.", err=True)
        raise typer.Exit(code=1)

    # Direct render when caller specified a target.
    if name_or_path is not None or task is not None:
        if web:
            try:
                path = find_spec(workspace_root, name_or_path, task=task, state=state)
            except SpecNotFoundError as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(code=1)
            _serve_web(path, port)
            return
        view = SpecView(
            workspace_root=workspace_root,
            name_or_path=name_or_path,
            task=task,
            state=state,
            watch=watch,
            interval=interval,
        )
        view.run()
        return

    # No target: open the cross-task spec index picker.
    from mship.cli.view._spec_index import SpecIndexApp
    app_ = SpecIndexApp(
        workspace_root=workspace_root, state=state, watch=watch, interval=interval,
    )
    app_.run()
```

Add these imports/options at the top of the file and to the command signature:

```python
from mship.core.view.task_index import find_all_specs
```

Add the new option to `def spec(...)`:

```python
task: Optional[str] = typer.Option(None, "--task", help="Narrow to one task's worktrees"),
```

Extract the existing web-serving block into a helper `_serve_web(path, port)`:

```python
def _serve_web(path: Path, port: int | None) -> None:
    try:
        server, chosen, _t = serve_spec_web(path, explicit_port=port)
    except NoFreePortError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)
    url = f"http://127.0.0.1:{chosen}/"
    typer.echo(f"Serving {path.name} at {url} (Ctrl-C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        import time as _t2
        while True:
            _t2.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
```

Update `SpecView.__init__` and `_refresh_content` to accept `task` + `state` kwargs and pass them to `find_spec`:

```python
def __init__(self, workspace_root, name_or_path, *, task=None, state=None, **kw):
    kw.pop("workspace_root", None); kw.pop("name_or_path", None)
    kw.pop("task", None); kw.pop("state", None)
    super().__init__(**kw)
    self._workspace_root = workspace_root
    self._name_or_path = name_or_path
    self._task = task
    self._state = state
    ...
```

And in `_refresh_content`, change the `find_spec` call to:

```python
path = find_spec(self._workspace_root, self._name_or_path, task=self._task, state=self._state)
```

- [ ] **Step 5.4: Create the spec-index app**

Create `src/mship/cli/view/_spec_index.py`:

```python
"""Cross-task spec picker: rows of specs across every worktree."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Markdown, Static
from textual.containers import VerticalScroll

from mship.cli.view._base import ViewApp
from mship.core.view.task_index import SpecEntry, find_all_specs


def _fmt_mtime(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


class SpecIndexApp(ViewApp):
    BINDINGS = ViewApp.BINDINGS + [
        Binding("enter", "open_cursor", "Open", show=True),
        Binding("escape", "back_to_index", "Back", show=True),
    ]

    def __init__(self, workspace_root: Path, state, **kw):
        super().__init__(**kw)
        self._workspace_root = workspace_root
        self._state = state
        self._entries: list[SpecEntry] = []
        self._table: DataTable | None = None
        self._markdown: Markdown | None = None
        self._body: VerticalScroll | None = None
        self._empty: Static | None = None
        self._mode: str = "index"  # "index" | "spec"

    def compose(self) -> ComposeResult:
        self._entries = find_all_specs(self._state, self._workspace_root)
        if not self._entries:
            self._empty = Static("No specs found in any task or main checkout.", expand=True)
            yield self._empty
            return
        self._table = DataTable(cursor_type="row")
        self._table.add_columns("task", "filename", "modified", "title")
        for e in self._entries:
            slug = e.task_slug or "—"
            self._table.add_row(slug, e.path.name, _fmt_mtime(e.mtime), e.title, key=str(e.path))
        self._markdown = Markdown("")
        self._body = VerticalScroll(self._markdown)
        self._body.display = False
        yield self._table
        yield self._body

    def on_mount(self) -> None:
        if self._table is not None:
            self._table.focus()
        if self._watch:
            self.set_interval(self._interval, self._refresh_index)

    def _refresh_index(self) -> None:
        if self._mode != "index" or self._table is None:
            return
        selected_key = None
        if self._table.cursor_row is not None and self._table.cursor_row < len(self._entries):
            selected_key = str(self._entries[self._table.cursor_row].path)
        self._entries = find_all_specs(self._state, self._workspace_root)
        self._table.clear()
        new_cursor = 0
        for i, e in enumerate(self._entries):
            slug = e.task_slug or "—"
            self._table.add_row(slug, e.path.name, _fmt_mtime(e.mtime), e.title, key=str(e.path))
            if str(e.path) == selected_key:
                new_cursor = i
        if self._entries:
            self._table.move_cursor(row=new_cursor)

    def action_open_cursor(self) -> None:
        if self._table is None or self._markdown is None or self._body is None:
            return
        if self._table.cursor_row is None or self._table.cursor_row >= len(self._entries):
            return
        entry = self._entries[self._table.cursor_row]
        try:
            self._markdown.update(entry.path.read_text())
        except OSError as e:
            self._markdown.update(f"Error reading spec: {e!r}")
        self._table.display = False
        self._body.display = True
        self._mode = "spec"
        self._body.focus()

    def action_back_to_index(self) -> None:
        if self._mode != "spec" or self._table is None or self._body is None:
            return
        self._body.display = False
        self._table.display = True
        self._mode = "index"
        self._table.focus()

    # Test helpers
    def row_filenames(self) -> list[str]:
        return [e.path.name for e in self._entries]

    def current_mode(self) -> str:
        return self._mode
```

- [ ] **Step 5.5: Run spec tests**

Run: `uv run pytest tests/cli/view/test_spec_view.py -v`
Expected: all PASS (old tests use `name_or_path` directly and still go through the non-index path).

- [ ] **Step 5.6: Commit**

```bash
git add src/mship/cli/view/spec.py src/mship/cli/view/_spec_index.py tests/cli/view/test_spec_view.py
git commit -m "feat(view): cross-task spec index picker"
```

---

## Task 6: Rewire `view logs` — picker + `--task` flag

**Files:**
- Modify: `src/mship/cli/view/logs.py`
- Test: `tests/cli/view/test_logs_view.py`

- [ ] **Step 6.1: Write failing test**

Append to `tests/cli/view/test_logs_view.py`:

```python
from typer.testing import CliRunner
from mship.cli import app


def test_logs_cli_rejects_unknown_task():
    runner = CliRunner()
    result = runner.invoke(app, ["view", "logs", "--task", "nope"])
    assert result.exit_code != 0
    assert "nope" in result.output
```

- [ ] **Step 6.2: Run test**

Run: `uv run pytest tests/cli/view/test_logs_view.py::test_logs_cli_rejects_unknown_task -v`
Expected: FAIL (`--task` not recognized, or no validation).

- [ ] **Step 6.3: Update `logs.py`**

Replace the `register()` function body in `src/mship/cli/view/logs.py`:

```python
def register(app: typer.Typer, get_container):
    @app.command()
    def logs(
        task: Optional[str] = typer.Option(None, "--task", help="Task slug (default: picker / current)"),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        all_: bool = typer.Option(False, "--all", help="Show all log entries, ignore active_repo"),
    ):
        """Live tail of a task's log (picker when no task specified)."""
        from pathlib import Path as _P

        container = get_container()
        state = container.state_manager().load()

        task_slug = task
        if task_slug is None and state.current_task is not None:
            task_slug = state.current_task

        if task_slug is not None:
            if task_slug not in state.tasks:
                known = ", ".join(sorted(state.tasks.keys())) or "(none)"
                typer.echo(f"Unknown task '{task_slug}'. Known: {known}.", err=True)
                raise typer.Exit(code=1)
            scope: Optional[str] = None
            if not all_ and state.current_task == task_slug:
                scope = state.tasks[task_slug].active_repo
            view = LogsView(
                state_manager=container.state_manager(),
                log_manager=container.log_manager(),
                task_slug=task_slug,
                scope_to_repo=scope,
                watch=watch,
                interval=interval,
            )
            view.run()
            return

        # No task + no current: show picker.
        from mship.cli.view._picker import TaskPicker, picker_rows
        from mship.core.view.task_index import build_task_index

        workspace_root = _P(container.config_path()).parent
        index = build_task_index(state, workspace_root)
        selected: dict[str, str] = {}
        def _on_select(slug: str) -> None:
            selected["slug"] = slug
        picker = TaskPicker(
            rows=picker_rows(index), on_select=_on_select, watch=False, interval=interval,
        )
        picker.run()
        chosen = selected.get("slug")
        if chosen is None:
            return
        view = LogsView(
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            task_slug=chosen,
            scope_to_repo=None,
            watch=watch,
            interval=interval,
        )
        view.run()
```

Also remove the old positional `task_slug: Optional[str] = typer.Argument(None, ...)`; `--task` replaces it. Update imports if needed.

- [ ] **Step 6.4: Run tests**

Run: `uv run pytest tests/cli/view/test_logs_view.py -v`
Expected: all PASS. (Note: any existing test that passed `task_slug` positionally must be updated to use `--task`.)

- [ ] **Step 6.5: Commit**

```bash
git add src/mship/cli/view/logs.py tests/cli/view/test_logs_view.py
git commit -m "feat(view): logs --task flag + task picker fallback"
```

---

## Task 7: Rewire `view diff` — picker + `--task`

**Files:**
- Modify: `src/mship/cli/view/diff.py`
- Test: `tests/cli/view/test_diff_view.py`

- [ ] **Step 7.1: Write failing test**

Append to `tests/cli/view/test_diff_view.py`:

```python
from typer.testing import CliRunner
from mship.cli import app


def test_diff_cli_rejects_unknown_task():
    runner = CliRunner()
    result = runner.invoke(app, ["view", "diff", "--task", "nope"])
    assert result.exit_code != 0
    assert "nope" in result.output
```

- [ ] **Step 7.2: Run test**

Run: `uv run pytest tests/cli/view/test_diff_view.py::test_diff_cli_rejects_unknown_task -v`
Expected: FAIL.

- [ ] **Step 7.3: Update `diff.py`**

Replace the `register()` function body in `src/mship/cli/view/diff.py`:

```python
def register(app: typer.Typer, get_container):
    @app.command()
    def diff(
        task: Optional[str] = typer.Option(None, "--task", help="Task slug (default: picker / current)"),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        all_: bool = typer.Option(False, "--all", help="Show all worktrees, ignore active_repo"),
    ):
        """Live per-worktree git diff (picker when no task specified)."""
        from pathlib import Path as _P
        container = get_container()
        state = container.state_manager().load()

        target_task = task if task is not None else state.current_task
        if task is not None and task not in state.tasks:
            known = ", ".join(sorted(state.tasks.keys())) or "(none)"
            typer.echo(f"Unknown task '{task}'. Known: {known}.", err=True)
            raise typer.Exit(code=1)

        def _resolver() -> tuple[list[Path], Path | None]:
            if target_task is not None and target_task in state.tasks:
                t = state.tasks[target_task]
                all_paths = [Path(p) for p in t.worktrees.values()]
                scope: Path | None = None
                if not all_ and t.active_repo is not None and t.active_repo in t.worktrees:
                    scope = Path(t.worktrees[t.active_repo])
                return all_paths, scope
            return [Path(repo.path) for repo in container.config().repos.values()], None

        if target_task is not None:
            view = DiffView(resolve_paths=_resolver, watch=watch, interval=interval)
            view.run()
            return

        # Picker flow.
        from mship.cli.view._picker import TaskPicker, picker_rows
        from mship.core.view.task_index import build_task_index

        workspace_root = _P(container.config_path()).parent
        index = build_task_index(state, workspace_root)
        selected: dict[str, str] = {}
        def _on_select(slug: str) -> None:
            selected["slug"] = slug
        picker = TaskPicker(rows=picker_rows(index), on_select=_on_select, watch=False, interval=interval)
        picker.run()
        chosen = selected.get("slug")
        if chosen is None:
            return

        def _resolver_for(slug: str):
            def inner() -> tuple[list[Path], Path | None]:
                t = state.tasks[slug]
                return [Path(p) for p in t.worktrees.values()], None
            return inner

        view = DiffView(resolve_paths=_resolver_for(chosen), watch=watch, interval=interval)
        view.run()
```

Import note: `from typing import Optional` if not already present.

- [ ] **Step 7.4: Run tests**

Run: `uv run pytest tests/cli/view/test_diff_view.py -v`
Expected: all PASS.

- [ ] **Step 7.5: Commit**

```bash
git add src/mship/cli/view/diff.py tests/cli/view/test_diff_view.py
git commit -m "feat(view): diff --task flag + task picker fallback"
```

---

## Task 8: Full suite + integration check

- [ ] **Step 8.1: Run the full test suite**

Run: `uv run pytest -x -q`
Expected: all tests PASS. If any fail, read the failure, fix the test or code, commit the fix, re-run.

- [ ] **Step 8.2: Smoke test interactively**

Run each (in a real terminal — `q` to quit each TUI):

```bash
mship view status            # should stack all tasks
mship view status --task <slug>
mship view spec              # should open spec index picker
mship view logs              # should open task picker
mship view diff              # should open task picker
```

- [ ] **Step 8.3: Final commit if fixes landed**

If Step 8.1 produced fixes, commit them with a message like `fix(view): stabilize tests after God-view rewire`.

- [ ] **Step 8.4: `mship finish`**

Run: `mship finish`
Expected: PR opened, task moves to finished state.

---

## Self-Review

**Spec coverage:**
- ✅ `status`, `spec`, `logs`, `diff` all default to cross-task view (Tasks 4, 5, 6, 7).
- ✅ `view spec` finds specs across every worktree (Task 2 `find_all_specs`).
- ✅ `--task <slug>` narrows each (Tasks 4–7).
- ✅ Finished-awaiting-close visible with marker (Task 3 `_flags_for`).
- ✅ Zellij panes don't break (Task 4 adds picker empty-state, Task 3 handles zero-rows case).
- ✅ No state-model change (we only read `state.tasks`).

**Placeholder scan:** no TBDs, no "add validation", no "similar to Task N" — all code shown inline.

**Type consistency:** `TaskSummary` shape used identically in `_picker.py`, `status.py`; `SpecEntry` shape used identically in `_spec_index.py`; `find_spec` signature extended compatibly.

**Risks flagged in spec:**
- Textual picker cursor preservation on refresh: handled in `_spec_index.py::_refresh_index` (preserves by path key).
- Spec title extraction: bounded 2048-byte read in `_extract_title`.
