# `mship view` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `mship view <subcommand>` read-only Textual TUIs (`status`, `logs`, `diff`, `spec`) designed for zellij/tmux panes — single-stream, scroll-preserving, `--watch` refreshes without yanking scroll position.

**Architecture:** New Typer sub-app under `src/mship/cli/view/`. Each view is a `textual.App` subclass that delegates all data-gathering to pure helpers in `src/mship/core/view/`. A shared `_base.py` provides the refresh loop, key bindings, alt-screen handling, and no-yank scroll behavior. Business logic stays testable without a TUI.

**Tech Stack:** Python 3.12+, Typer, Textual (built on rich, already installed), stdlib `http.server` for `spec --web`, optional `delta` binary detection for diff rendering.

**Spec:** `docs/superpowers/specs/2026-04-13-mship-view-design.md`

---

## File Structure

**Create:**
- `src/mship/cli/view/__init__.py` — Typer sub-app, registers subcommands
- `src/mship/cli/view/_base.py` — Shared `ViewApp` base class (refresh loop, keys, alt-screen, no-yank scroll)
- `src/mship/cli/view/status.py` — `mship view status`
- `src/mship/cli/view/logs.py` — `mship view logs`
- `src/mship/cli/view/diff.py` — `mship view diff`
- `src/mship/cli/view/spec.py` — `mship view spec`, includes `--web` server
- `src/mship/core/view/__init__.py` — empty
- `src/mship/core/view/diff_sources.py` — per-worktree diff collection + untracked-file synthesis
- `src/mship/core/view/spec_discovery.py` — spec file location and resolution
- `src/mship/core/view/web_port.py` — port selection for `--web`
- `tests/cli/view/` + `tests/core/view/` — test mirrors

**Modify:**
- `pyproject.toml` — add `textual>=0.80` dep
- `src/mship/cli/__init__.py` — register view sub-app

---

## Task 1: Add Textual dependency and register empty view sub-app

**Files:**
- Modify: `pyproject.toml`
- Create: `src/mship/cli/view/__init__.py`
- Modify: `src/mship/cli/__init__.py:56-73`
- Test: `tests/cli/view/test_view_registration.py`

- [ ] **Step 1: Write the failing test**

`tests/cli/view/test_view_registration.py`:
```python
from typer.testing import CliRunner
from mship.cli import app

runner = CliRunner()


def test_view_command_exists():
    result = runner.invoke(app, ["view", "--help"])
    assert result.exit_code == 0
    assert "status" in result.stdout
    assert "logs" in result.stdout
    assert "diff" in result.stdout
    assert "spec" in result.stdout
```

Also create empty `tests/cli/view/__init__.py`.

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/cli/view/test_view_registration.py -v`
Expected: FAIL — "No such command 'view'".

- [ ] **Step 3: Add textual dep**

In `pyproject.toml`, under `dependencies = [`, add `"textual>=0.80",` after `"rich>=13.0",`.

Run: `uv sync`

- [ ] **Step 4: Create the view sub-app skeleton**

`src/mship/cli/view/__init__.py`:
```python
import typer

app = typer.Typer(name="view", help="Read-only live views for tmux/zellij panes")


def register(parent: typer.Typer, get_container):
    from mship.cli.view import status as _status
    from mship.cli.view import logs as _logs
    from mship.cli.view import diff as _diff
    from mship.cli.view import spec as _spec

    _status.register(app, get_container)
    _logs.register(app, get_container)
    _diff.register(app, get_container)
    _spec.register(app, get_container)

    parent.add_typer(app, name="view")
```

Create placeholder files so imports succeed:

`src/mship/cli/view/status.py`, `logs.py`, `diff.py`, `spec.py` — each with:
```python
import typer


def register(app: typer.Typer, get_container):
    @app.command()
    def __placeholder__():
        """Placeholder; replaced in later task."""
        raise NotImplementedError
```

Rename `__placeholder__` per file: `status`, `logs`, `diff`, `spec`.

- [ ] **Step 5: Wire sub-app into root CLI**

In `src/mship/cli/__init__.py`, after the other `_mod.register(...)` calls, add:
```python
from mship.cli import view as _view_mod
_view_mod.register(app, get_container)
```

- [ ] **Step 6: Run test, verify it passes**

Run: `uv run pytest tests/cli/view/test_view_registration.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/mship/cli/__init__.py src/mship/cli/view tests/cli/view
git commit -m "feat: scaffold mship view sub-app with placeholder subcommands"
```

---

## Task 2: Spec discovery helper

**Files:**
- Create: `src/mship/core/view/__init__.py` (empty)
- Create: `src/mship/core/view/spec_discovery.py`
- Test: `tests/core/view/test_spec_discovery.py`

- [ ] **Step 1: Write the failing test**

`tests/core/view/test_spec_discovery.py`:
```python
import os
import time
from pathlib import Path
import pytest

from mship.core.view.spec_discovery import find_spec, SpecNotFoundError


def _touch(path: Path, mtime: float) -> None:
    path.write_text("# test\n")
    os.utime(path, (mtime, mtime))


def test_find_newest_by_mtime(tmp_path: Path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    _touch(specs / "2026-04-11-a.md", time.time() - 100)
    _touch(specs / "2026-04-12-b.md", time.time() - 10)
    _touch(specs / "2026-04-10-c.md", time.time() - 200)

    result = find_spec(workspace_root=tmp_path, name_or_path=None)
    assert result.name == "2026-04-12-b.md"


def test_find_by_name_with_extension(tmp_path: Path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "foo.md").write_text("# foo")
    result = find_spec(workspace_root=tmp_path, name_or_path="foo.md")
    assert result.name == "foo.md"


def test_find_by_name_without_extension(tmp_path: Path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "foo.md").write_text("# foo")
    result = find_spec(workspace_root=tmp_path, name_or_path="foo")
    assert result.name == "foo.md"


def test_find_by_absolute_path(tmp_path: Path):
    f = tmp_path / "custom.md"
    f.write_text("# custom")
    result = find_spec(workspace_root=tmp_path, name_or_path=str(f))
    assert result == f


def test_empty_specs_dir_raises(tmp_path: Path):
    (tmp_path / "docs" / "superpowers" / "specs").mkdir(parents=True)
    with pytest.raises(SpecNotFoundError):
        find_spec(workspace_root=tmp_path, name_or_path=None)


def test_missing_specs_dir_raises(tmp_path: Path):
    with pytest.raises(SpecNotFoundError):
        find_spec(workspace_root=tmp_path, name_or_path=None)


def test_missing_named_spec_raises(tmp_path: Path):
    (tmp_path / "docs" / "superpowers" / "specs").mkdir(parents=True)
    with pytest.raises(SpecNotFoundError):
        find_spec(workspace_root=tmp_path, name_or_path="does-not-exist")
```

Also create empty `tests/core/view/__init__.py`.

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/core/view/test_spec_discovery.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`src/mship/core/view/__init__.py`: empty.

`src/mship/core/view/spec_discovery.py`:
```python
from pathlib import Path


class SpecNotFoundError(Exception):
    pass


SPEC_SUBDIR = Path("docs") / "superpowers" / "specs"


def find_spec(workspace_root: Path, name_or_path: str | None) -> Path:
    """Resolve a spec file. None = newest by mtime in default spec dir."""
    if name_or_path is None:
        return _newest(workspace_root / SPEC_SUBDIR)

    candidate = Path(name_or_path)
    if candidate.is_absolute() and candidate.is_file():
        return candidate

    specs_dir = workspace_root / SPEC_SUBDIR
    for name in (name_or_path, f"{name_or_path}.md"):
        p = specs_dir / name
        if p.is_file():
            return p

    raise SpecNotFoundError(
        f"Spec not found: {name_or_path!r} (checked {specs_dir})"
    )


def _newest(specs_dir: Path) -> Path:
    if not specs_dir.is_dir():
        raise SpecNotFoundError(f"Spec directory does not exist: {specs_dir}")
    candidates = [p for p in specs_dir.iterdir() if p.is_file() and p.suffix == ".md"]
    if not candidates:
        raise SpecNotFoundError(f"No specs found in {specs_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/core/view/test_spec_discovery.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/view tests/core/view
git commit -m "feat: add spec_discovery helper for mship view spec"
```

---

## Task 3: Web port scanner

**Files:**
- Create: `src/mship/core/view/web_port.py`
- Test: `tests/core/view/test_web_port.py`

- [ ] **Step 1: Write the failing test**

`tests/core/view/test_web_port.py`:
```python
import socket
import pytest

from mship.core.view.web_port import (
    pick_port,
    NoFreePortError,
    DEFAULT_START_PORT,
    BLOCKED_DEV_PORTS,
)


def _occupy(port: int) -> socket.socket:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def test_default_start_is_uncommon():
    assert DEFAULT_START_PORT >= 40000
    assert DEFAULT_START_PORT not in BLOCKED_DEV_PORTS


def test_picks_default_when_free():
    assert pick_port() == DEFAULT_START_PORT


def test_skips_occupied_port():
    s = _occupy(DEFAULT_START_PORT)
    try:
        assert pick_port() == DEFAULT_START_PORT + 1
    finally:
        s.close()


def test_skips_blocked_dev_ports():
    assert pick_port(start=3000) != 3000
    assert pick_port(start=3000) not in BLOCKED_DEV_PORTS


def test_honors_explicit_port():
    assert pick_port(explicit=DEFAULT_START_PORT) == DEFAULT_START_PORT


def test_explicit_port_in_use_raises():
    s = _occupy(DEFAULT_START_PORT)
    try:
        with pytest.raises(NoFreePortError):
            pick_port(explicit=DEFAULT_START_PORT)
    finally:
        s.close()


def test_exhausted_scan_raises():
    # Scan a tiny range that's fully blocked: all ports in blocklist
    with pytest.raises(NoFreePortError):
        pick_port(start=3000, max_tries=1)
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/core/view/test_web_port.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`src/mship/core/view/web_port.py`:
```python
import socket

DEFAULT_START_PORT = 47213
BLOCKED_DEV_PORTS = frozenset(
    {3000, 3001, 4200, 5000, 5173, 8000, 8080, 8443, 8888, 9000}
)


class NoFreePortError(Exception):
    pass


def _is_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(
    start: int = DEFAULT_START_PORT,
    max_tries: int = 10,
    explicit: int | None = None,
) -> int:
    if explicit is not None:
        if _is_free(explicit):
            return explicit
        raise NoFreePortError(f"Port {explicit} is in use")

    port = start
    tried = 0
    while tried < max_tries:
        if port not in BLOCKED_DEV_PORTS and _is_free(port):
            return port
        port += 1
        tried += 1
    raise NoFreePortError(
        f"No free port found in {max_tries} tries starting at {start}"
    )
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/core/view/test_web_port.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/view/web_port.py tests/core/view/test_web_port.py
git commit -m "feat: add web_port picker for mship view spec --web"
```

---

## Task 4: Diff sources — per-worktree diff + untracked synthesis

**Files:**
- Create: `src/mship/core/view/diff_sources.py`
- Test: `tests/core/view/test_diff_sources.py`

- [ ] **Step 1: Write the failing test**

`tests/core/view/test_diff_sources.py`:
```python
import subprocess
from pathlib import Path

from mship.core.view.diff_sources import (
    synthesize_untracked_diff,
    collect_worktree_diff,
    WorktreeDiff,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "seed.txt").write_text("seed\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "seed")


def test_synthesize_untracked_text_file(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "new.py").write_text("print('hi')\nprint('bye')\n")
    out = synthesize_untracked_diff(tmp_path, Path("new.py"))
    assert "+++ b/new.py" in out
    assert "+print('hi')" in out
    assert "+print('bye')" in out
    assert "new file mode" in out


def test_synthesize_untracked_binary_stub(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    out = synthesize_untracked_diff(tmp_path, Path("blob.bin"))
    assert "new binary file" in out
    assert "4 bytes" in out


def test_synthesize_untracked_empty_file(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "empty.txt").write_text("")
    out = synthesize_untracked_diff(tmp_path, Path("empty.txt"))
    assert "new file mode" in out
    assert "+++ b/empty.txt" in out


def test_collect_includes_modified_and_untracked(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\nchanged\n")
    (tmp_path / "added.txt").write_text("added\n")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "x.txt").write_text("x")

    result = collect_worktree_diff(tmp_path)
    assert isinstance(result, WorktreeDiff)
    assert "changed" in result.combined
    assert "+++ b/added.txt" in result.combined
    assert "ignored/x.txt" not in result.combined
    assert result.files_changed >= 2


def test_collect_clean_worktree_is_empty(tmp_path: Path):
    _init_repo(tmp_path)
    result = collect_worktree_diff(tmp_path)
    assert result.combined == ""
    assert result.files_changed == 0
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/core/view/test_diff_sources.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`src/mship/core/view/diff_sources.py`:
```python
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorktreeDiff:
    root: Path
    combined: str
    files_changed: int


def _is_binary(content: bytes) -> bool:
    return b"\0" in content[:8000]


def synthesize_untracked_diff(worktree: Path, rel_path: Path) -> str:
    """Return a 'new file' diff header+hunks for an untracked file.

    Binary files get a stub line.
    """
    abs_path = worktree / rel_path
    data = abs_path.read_bytes()
    header = (
        f"diff --git a/{rel_path} b/{rel_path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{rel_path}\n"
    )
    if _is_binary(data):
        return f"{header}new binary file, {len(data)} bytes\n"

    if not data:
        return header
    lines = data.decode("utf-8", errors="replace").splitlines()
    hunk = f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    return header + hunk


def _list_untracked(worktree: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    raw = result.stdout.decode("utf-8", errors="replace")
    return [Path(p) for p in raw.split("\0") if p]


def _tracked_diff(worktree: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def collect_worktree_diff(worktree: Path) -> WorktreeDiff:
    tracked = _tracked_diff(worktree)
    untracked = _list_untracked(worktree)
    synthesized = "".join(synthesize_untracked_diff(worktree, p) for p in untracked)
    combined = tracked + synthesized
    files_changed = combined.count("\ndiff --git ") + (1 if combined.startswith("diff --git ") else 0)
    return WorktreeDiff(root=worktree, combined=combined, files_changed=files_changed)
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/core/view/test_diff_sources.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/view/diff_sources.py tests/core/view/test_diff_sources.py
git commit -m "feat: add diff_sources with untracked-file diff synthesis"
```

---

## Task 5: Base `ViewApp` class — shared refresh, keys, scroll behavior

**Files:**
- Create: `src/mship/cli/view/_base.py`
- Test: `tests/cli/view/test_base.py`

- [ ] **Step 1: Write the failing test**

`tests/cli/view/test_base.py`:
```python
import pytest

from mship.cli.view._base import ViewApp


class _CountingView(ViewApp):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.renders = 0
        self.content = "line 1\nline 2\nline 3\n"

    def gather(self) -> str:
        self.renders += 1
        return self.content


@pytest.mark.asyncio
async def test_initial_render():
    app = _CountingView(watch=False, interval=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.renders == 1
        assert "line 1" in app.rendered_text()


@pytest.mark.asyncio
async def test_watch_mode_refreshes():
    app = _CountingView(watch=True, interval=0.05)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        assert app.renders >= 2


@pytest.mark.asyncio
async def test_quit_key():
    app = _CountingView(watch=False, interval=0.05)
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
        assert not app.is_running


@pytest.mark.asyncio
async def test_scroll_position_preserved_across_refresh():
    app = _CountingView(watch=True, interval=0.05)
    app.content = "\n".join(f"line {i}" for i in range(200)) + "\n"
    async with app.run_test() as pilot:
        await pilot.pause()
        app.scroll_body_to(50)
        y_before = app.body_scroll_y()
        app.content = "\n".join(f"line {i}" for i in range(201)) + "\n"  # grew
        await pilot.pause(0.15)
        assert app.body_scroll_y() == y_before, "should not yank when user scrolled away"
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/cli/view/test_base.py -v`
Expected: FAIL — module not found.

(If pytest-asyncio is not installed, install it: add `pytest-asyncio>=0.23` to dev deps in `pyproject.toml` and `uv sync`. Check first with `uv run python -c "import pytest_asyncio"` — if it imports, skip this.)

- [ ] **Step 3: Implement base**

`src/mship/cli/view/_base.py`:
```python
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static


class ViewApp(App):
    """Base class for mship view TUIs.

    Subclasses override `gather()` to return the text body for the view.
    Watch-mode polls `gather()` on `interval` seconds and updates the body
    widget in place, preserving scroll position unless the user is pinned
    to the bottom (auto-follow).
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("r", "force_refresh", "Refresh"),
        Binding("j,down", "scroll_down", "Down", show=False),
        Binding("k,up", "scroll_up", "Up", show=False),
        Binding("pagedown", "page_down", "PgDn", show=False),
        Binding("pageup", "page_up", "PgUp", show=False),
        Binding("home", "scroll_home", "Home", show=False),
        Binding("end", "scroll_end", "End", show=False),
    ]

    def __init__(self, watch: bool = False, interval: float = 2.0, **kw) -> None:
        super().__init__(**kw)
        self._watch = watch
        self._interval = interval
        self._body: VerticalScroll | None = None
        self._static: Static | None = None

    # --- subclass hook ---
    def gather(self) -> str:
        raise NotImplementedError

    # --- Textual lifecycle ---
    def compose(self) -> ComposeResult:
        self._static = Static("", expand=True)
        self._body = VerticalScroll(self._static)
        yield self._body

    def on_mount(self) -> None:
        self._refresh_content()
        if self._watch:
            self.set_interval(self._interval, self._refresh_content)

    def _refresh_content(self) -> None:
        assert self._static is not None and self._body is not None
        was_at_end = self._body.scroll_y >= (self._body.max_scroll_y - 1)
        prev_y = self._body.scroll_y
        try:
            text = self.gather()
        except Exception as e:
            text = f"[error gathering content] {e!r}"
        self._static.update(text)
        self.call_after_refresh(self._restore_scroll, prev_y, was_at_end)

    def _restore_scroll(self, prev_y: float, was_at_end: bool) -> None:
        assert self._body is not None
        if was_at_end:
            self._body.scroll_end(animate=False)
        else:
            self._body.scroll_to(y=prev_y, animate=False)

    # --- actions ---
    def action_force_refresh(self) -> None:
        self._refresh_content()

    def action_scroll_down(self) -> None:
        assert self._body is not None
        self._body.scroll_relative(y=1, animate=False)

    def action_scroll_up(self) -> None:
        assert self._body is not None
        self._body.scroll_relative(y=-1, animate=False)

    def action_page_down(self) -> None:
        assert self._body is not None
        self._body.scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        assert self._body is not None
        self._body.scroll_page_up(animate=False)

    def action_scroll_home(self) -> None:
        assert self._body is not None
        self._body.scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        assert self._body is not None
        self._body.scroll_end(animate=False)

    # --- test helpers ---
    def rendered_text(self) -> str:
        assert self._static is not None
        return str(self._static.renderable)

    def body_scroll_y(self) -> float:
        assert self._body is not None
        return self._body.scroll_y

    def scroll_body_to(self, y: float) -> None:
        assert self._body is not None
        self._body.scroll_to(y=y, animate=False)
```

- [ ] **Step 4: Add pytest-asyncio config if missing**

Check `pyproject.toml` `[tool.pytest.ini_options]`. Ensure it contains `asyncio_mode = "auto"` (or add a `[tool.pytest.ini_options]` block with it). If `pytest-asyncio` isn't in deps, add `"pytest-asyncio>=0.23"` alongside `pytest` and run `uv sync`.

- [ ] **Step 5: Run test, verify it passes**

Run: `uv run pytest tests/cli/view/test_base.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/view/_base.py tests/cli/view/test_base.py pyproject.toml uv.lock
git commit -m "feat: add ViewApp base class with no-yank scroll and watch loop"
```

---

## Task 6: `mship view status` — workspace snapshot

**Files:**
- Modify: `src/mship/cli/view/status.py`
- Test: `tests/cli/view/test_status_view.py`

- [ ] **Step 1: Write the failing test**

`tests/cli/view/test_status_view.py`:
```python
import pytest
from pathlib import Path

from mship.cli.view.status import StatusView


class _FakeTask:
    slug = "t1"
    phase = "impl"
    blocked_reason = None
    blocked_at = None
    branch = "feat/x"
    affected_repos = ["repo-a", "repo-b"]
    worktrees = {"repo-a": "/tmp/wta", "repo-b": "/tmp/wtb"}
    test_results = {}


class _FakeState:
    current_task = "t1"
    tasks = {"t1": _FakeTask()}


class _FakeStateManager:
    def load(self):
        return _FakeState()


@pytest.mark.asyncio
async def test_status_view_renders_task():
    view = StatusView(state_manager=_FakeStateManager(), watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "t1" in text
        assert "impl" in text
        assert "repo-a" in text


@pytest.mark.asyncio
async def test_status_view_no_active_task():
    class _Empty:
        current_task = None
        tasks = {}

    class _Mgr:
        def load(self):
            return _Empty()

    view = StatusView(state_manager=_Mgr(), watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No active task" in view.rendered_text()
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/cli/view/test_status_view.py -v`
Expected: FAIL — `StatusView` not defined.

- [ ] **Step 3: Implement**

Replace `src/mship/cli/view/status.py`:
```python
import typer

from mship.cli.view._base import ViewApp


class StatusView(ViewApp):
    def __init__(self, state_manager, **kw):
        super().__init__(**kw)
        self._state_manager = state_manager

    def gather(self) -> str:
        state = self._state_manager.load()
        if state.current_task is None:
            return "No active task"
        task = state.tasks[state.current_task]
        lines = [
            f"Task:   {task.slug}",
            f"Phase:  {task.phase}"
            + (f"  (BLOCKED: {task.blocked_reason})" if task.blocked_reason else ""),
            f"Branch: {task.branch}",
            f"Repos:  {', '.join(task.affected_repos)}",
        ]
        if task.worktrees:
            lines.append("Worktrees:")
            for repo, path in task.worktrees.items():
                lines.append(f"  {repo}: {path}")
        if task.test_results:
            lines.append("Tests:")
            for repo, result in task.test_results.items():
                lines.append(f"  {repo}: {result.status}")
        return "\n".join(lines)


def register(app: typer.Typer, get_container):
    @app.command()
    def status(
        watch: bool = typer.Option(False, "--watch", help="Refresh on interval"),
        interval: float = typer.Option(2.0, "--interval", help="Refresh seconds"),
    ):
        """Live workspace status view."""
        container = get_container()
        view = StatusView(
            state_manager=container.state_manager(),
            watch=watch,
            interval=interval,
        )
        view.run()
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/cli/view/test_status_view.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/view/status.py tests/cli/view/test_status_view.py
git commit -m "feat: mship view status renders live workspace snapshot"
```

---

## Task 7: `mship view logs` — tails current task log

**Files:**
- Modify: `src/mship/cli/view/logs.py`
- Test: `tests/cli/view/test_logs_view.py`

- [ ] **Step 1: Write the failing test**

`tests/cli/view/test_logs_view.py`:
```python
import pytest
from dataclasses import dataclass
from datetime import datetime, timezone

from mship.cli.view.logs import LogsView


@dataclass
class _Entry:
    timestamp: datetime
    message: str


class _FakeLogMgr:
    def __init__(self, entries):
        self.entries = entries

    def read(self, slug, last=None):
        return list(self.entries)


class _FakeState:
    def __init__(self, slug):
        self.current_task = slug
        self.tasks = {slug: None} if slug else {}


class _FakeStateMgr:
    def __init__(self, slug="t1"):
        self._slug = slug

    def load(self):
        return _FakeState(self._slug)


@pytest.mark.asyncio
async def test_logs_view_renders_entries():
    entries = [
        _Entry(datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc), "hello"),
        _Entry(datetime(2026, 4, 13, 10, 5, tzinfo=timezone.utc), "world"),
    ]
    view = LogsView(
        state_manager=_FakeStateMgr(),
        log_manager=_FakeLogMgr(entries),
        task_slug=None,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "hello" in text
        assert "world" in text


@pytest.mark.asyncio
async def test_logs_view_no_task():
    view = LogsView(
        state_manager=_FakeStateMgr(slug=None),
        log_manager=_FakeLogMgr([]),
        task_slug=None,
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No active task" in view.rendered_text()


@pytest.mark.asyncio
async def test_logs_view_explicit_slug():
    entries = [_Entry(datetime(2026, 4, 13, tzinfo=timezone.utc), "specific")]
    view = LogsView(
        state_manager=_FakeStateMgr(slug=None),
        log_manager=_FakeLogMgr(entries),
        task_slug="other-task",
        watch=False,
        interval=1.0,
    )
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "specific" in view.rendered_text()
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/cli/view/test_logs_view.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Replace `src/mship/cli/view/logs.py`:
```python
from typing import Optional

import typer

from mship.cli.view._base import ViewApp


class LogsView(ViewApp):
    def __init__(self, state_manager, log_manager, task_slug: Optional[str], **kw):
        super().__init__(**kw)
        self._state_manager = state_manager
        self._log_manager = log_manager
        self._task_slug = task_slug

    def _resolve_slug(self) -> Optional[str]:
        if self._task_slug is not None:
            return self._task_slug
        state = self._state_manager.load()
        return state.current_task

    def gather(self) -> str:
        slug = self._resolve_slug()
        if slug is None:
            return "No active task (and no slug provided)"
        entries = self._log_manager.read(slug)
        if not entries:
            return f"Log for {slug} is empty"
        lines = []
        for entry in entries:
            ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"{ts}  {entry.message}")
        return "\n".join(lines)


def register(app: typer.Typer, get_container):
    @app.command()
    def logs(
        task_slug: Optional[str] = typer.Argument(None, help="Task slug (default: current)"),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
    ):
        """Live tail of a task's log."""
        container = get_container()
        view = LogsView(
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            task_slug=task_slug,
            watch=watch,
            interval=interval,
        )
        view.run()
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/cli/view/test_logs_view.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/view/logs.py tests/cli/view/test_logs_view.py
git commit -m "feat: mship view logs tails current task log"
```

---

## Task 8: `mship view diff` — per-worktree diff with untracked inline

**Files:**
- Modify: `src/mship/cli/view/diff.py`
- Test: `tests/cli/view/test_diff_view.py`

- [ ] **Step 1: Write the failing test**

`tests/cli/view/test_diff_view.py`:
```python
import subprocess
from pathlib import Path

import pytest

from mship.cli.view.diff import DiffView


def _init_repo(path: Path, seed: str = "seed\n") -> None:
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "seed.txt").write_text(seed)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_diff_view_shows_untracked_and_modified(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\nmore\n")
    (tmp_path / "new.py").write_text("print('hi')\n")

    view = DiffView(worktree_paths=[tmp_path], watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert "+more" in text
        assert "+print('hi')" in text


@pytest.mark.asyncio
async def test_diff_view_clean_worktree_shows_clean_marker(tmp_path: Path):
    _init_repo(tmp_path)
    view = DiffView(worktree_paths=[tmp_path], watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "clean" in view.rendered_text().lower()


@pytest.mark.asyncio
async def test_diff_view_multiple_worktrees_show_headers(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _init_repo(a)
    _init_repo(b)
    (a / "x.txt").write_text("x")
    view = DiffView(worktree_paths=[a, b], watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        text = view.rendered_text()
        assert str(a) in text
        assert str(b) in text
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/cli/view/test_diff_view.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Replace `src/mship/cli/view/diff.py`:
```python
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

import typer

from mship.cli.view._base import ViewApp
from mship.core.view.diff_sources import collect_worktree_diff


class DiffView(ViewApp):
    def __init__(self, worktree_paths: Iterable[Path], use_delta: bool | None = None, **kw):
        super().__init__(**kw)
        self._paths = list(worktree_paths)
        if use_delta is None:
            use_delta = shutil.which("delta") is not None
        self._use_delta = use_delta

    def _render_body(self, body: str) -> str:
        if not self._use_delta or not body:
            return body
        try:
            result = subprocess.run(
                ["delta", "--color-only"],
                input=body,
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            return result.stdout
        except (subprocess.SubprocessError, FileNotFoundError):
            return body

    def gather(self) -> str:
        if not self._paths:
            return "No worktrees configured"
        sections: list[str] = []
        for p in self._paths:
            try:
                wd = collect_worktree_diff(p)
            except subprocess.CalledProcessError as e:
                sections.append(f"▶ {p}  (error: {e})")
                continue
            if wd.files_changed == 0:
                sections.append(f"▶ {p}  (clean)")
                continue
            header = f"▶ {p}  ·  {wd.files_changed} files"
            body = self._render_body(wd.combined)
            sections.append(f"{header}\n{body}")
        return "\n\n".join(sections)


def _collect_workspace_worktrees(container) -> list[Path]:
    """All worktree paths for the current task, plus repo roots if no task."""
    state = container.state_manager().load()
    if state.current_task and state.current_task in state.tasks:
        task = state.tasks[state.current_task]
        paths = [Path(p) for p in task.worktrees.values()]
        if paths:
            return paths
    return [Path(repo.path) for repo in container.config().repos.values()]


def register(app: typer.Typer, get_container):
    @app.command()
    def diff(
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
    ):
        """Live per-worktree git diff, untracked files inline."""
        container = get_container()
        view = DiffView(
            worktree_paths=_collect_workspace_worktrees(container),
            watch=watch,
            interval=interval,
        )
        view.run()
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/cli/view/test_diff_view.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/view/diff.py tests/cli/view/test_diff_view.py
git commit -m "feat: mship view diff renders per-worktree diffs with untracked inline"
```

---

## Task 9: `mship view spec` — Markdown TUI + `--web` server

**Files:**
- Modify: `src/mship/cli/view/spec.py`
- Test: `tests/cli/view/test_spec_view.py`

- [ ] **Step 1: Write the failing test**

`tests/cli/view/test_spec_view.py`:
```python
import threading
import urllib.request
from pathlib import Path

import pytest

from mship.cli.view.spec import SpecView, serve_spec_web


@pytest.mark.asyncio
async def test_spec_view_renders_markdown(tmp_path: Path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "s.md").write_text("# Hello\n\nBody text.\n")
    view = SpecView(workspace_root=tmp_path, name_or_path=None, watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        # SpecView uses Markdown widget; body text should appear
        assert "Body text" in view.rendered_text()


@pytest.mark.asyncio
async def test_spec_view_missing_spec(tmp_path: Path):
    view = SpecView(workspace_root=tmp_path, name_or_path="nope", watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "Spec not found" in view.rendered_text()


def test_serve_spec_web_serves_rendered_html(tmp_path: Path):
    spec = tmp_path / "s.md"
    spec.write_text("# Title\n\nBody.\n")
    server, port, thread = serve_spec_web(spec, start_port=47500)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            html = r.read().decode("utf-8")
        assert "<h1>" in html.lower() or "title" in html.lower()
        assert "body" in html.lower()
    finally:
        server.shutdown()
        thread.join(timeout=2)
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/cli/view/test_spec_view.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Replace `src/mship/cli/view/spec.py`:
```python
from __future__ import annotations

import html
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import typer
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static

from mship.cli.view._base import ViewApp
from mship.core.view.spec_discovery import SpecNotFoundError, find_spec
from mship.core.view.web_port import NoFreePortError, pick_port


class SpecView(ViewApp):
    def __init__(
        self,
        workspace_root: Path,
        name_or_path: Optional[str],
        **kw,
    ):
        super().__init__(**kw)
        self._workspace_root = workspace_root
        self._name_or_path = name_or_path
        self._markdown: Markdown | None = None
        self._static: Static | None = None
        self._body: VerticalScroll | None = None
        self._error: str | None = None

    def compose(self) -> ComposeResult:
        self._markdown = Markdown("")
        self._static = Static("", expand=True)
        self._body = VerticalScroll(self._markdown, self._static)
        yield self._body

    def gather(self) -> str:  # not used; override refresh directly
        return ""

    def _refresh_content(self) -> None:
        assert self._markdown is not None and self._static is not None and self._body is not None
        was_at_end = self._body.scroll_y >= (self._body.max_scroll_y - 1)
        prev_y = self._body.scroll_y
        try:
            path = find_spec(self._workspace_root, self._name_or_path)
            self._markdown.update(path.read_text())
            self._static.update("")
            self._error = None
        except SpecNotFoundError as e:
            self._error = str(e)
            self._markdown.update("")
            self._static.update(f"Spec not found: {e}")
        self.call_after_refresh(self._restore_scroll, prev_y, was_at_end)

    def rendered_text(self) -> str:
        # test helper — concat both widgets' text
        md_text = ""
        if self._markdown is not None:
            md_text = str(getattr(self._markdown, "_markdown", "") or "")
        static_text = str(self._static.renderable) if self._static else ""
        return md_text + "\n" + static_text


def _render_html(spec_path: Path) -> bytes:
    try:
        from markdown_it import MarkdownIt  # type: ignore
        body_html = MarkdownIt().render(spec_path.read_text())
    except ImportError:
        body_html = f"<pre>{html.escape(spec_path.read_text())}</pre>"
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(spec_path.name)}</title>
<style>body{{font-family:system-ui;max-width:780px;margin:2rem auto;padding:0 1rem;line-height:1.55}}
pre,code{{background:#f4f4f4;padding:.1em .3em;border-radius:3px}}
pre{{padding:.6em;overflow:auto}}</style>
</head><body>{body_html}</body></html>"""
    return doc.encode("utf-8")


def serve_spec_web(
    spec_path: Path,
    start_port: int = None,
    explicit_port: int | None = None,
) -> tuple[HTTPServer, int, threading.Thread]:
    port = pick_port(
        start=start_port if start_port is not None else 47213,
        explicit=explicit_port,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = _render_html(spec_path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a, **kw):
            return

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


def register(app: typer.Typer, get_container):
    @app.command()
    def spec(
        name_or_path: Optional[str] = typer.Argument(None),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        web: bool = typer.Option(False, "--web", help="Serve rendered HTML on localhost"),
        port: Optional[int] = typer.Option(None, "--port", help="Explicit port for --web"),
    ):
        """Render a spec file (newest by default)."""
        from pathlib import Path as _P
        container = get_container()
        workspace_root = _P(container.config_path()).parent

        if web:
            try:
                path = find_spec(workspace_root, name_or_path)
            except SpecNotFoundError as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(code=1)
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
            return

        view = SpecView(
            workspace_root=workspace_root,
            name_or_path=name_or_path,
            watch=watch,
            interval=interval,
        )
        view.run()
```

- [ ] **Step 4: Ensure `markdown-it-py` is available**

Check: `uv run python -c "import markdown_it"`. If it fails, add `"markdown-it-py>=3.0"` to `pyproject.toml` dependencies and run `uv sync`.

- [ ] **Step 5: Run test, verify it passes**

Run: `uv run pytest tests/cli/view/test_spec_view.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/view/spec.py tests/cli/view/test_spec_view.py pyproject.toml uv.lock
git commit -m "feat: mship view spec with Markdown TUI and --web server"
```

---

## Task 10: Full-suite regression check and README mention

**Files:**
- Modify: `README.md` (tiny section under existing command reference)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -v`
Expected: all previously-passing tests + all new tests pass.

- [ ] **Step 2: Smoke-test each view manually**

Run each in a terminal, verify they launch and `q` quits cleanly:
```
uv run mship view status
uv run mship view diff
uv run mship view spec
uv run mship view logs
```

Note any that fail; fix before proceeding.

- [ ] **Step 3: Add short README section**

In `README.md`, find the commands section and add a `### Live views` subsection:

```markdown
### Live views

`mship view` provides read-only TUIs designed for tmux/zellij panes. All views support `--watch` and `--interval N`.

- `mship view status [--watch]` — current task, phase, worktrees, tests
- `mship view logs [task-slug] [--watch]` — tail of the task log
- `mship view diff [--watch]` — per-worktree git diff with untracked files inline
- `mship view spec [name-or-path] [--watch] [--web]` — render newest spec; `--web` serves HTML on localhost

Keys: `q` quit, `j/k` or arrows to scroll, `PgUp/PgDn`, `Home/End`, `r` force refresh.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document mship view commands"
```

---

## Self-Review

**Spec coverage:**
- Architecture (`cli/view/` + `core/view/`): Task 1 scaffolds, Tasks 2–4 core helpers, Task 5 base class, 6–9 implement each view. ✓
- Common flags (`--watch`, `--interval`, alt-screen, keys, no-yank scroll): Task 5 (`ViewApp`). ✓ (note: `--no-alt-screen` flag not exposed yet — Textual runs in alt-screen by default. If needed it's a one-liner `app.run(alternate_screen=False)` pass-through. Acceptable to defer.)
- `status` view: Task 6. ✓
- `logs` view (task log source per revised spec): Task 7. ✓
- `diff` with inline untracked, `delta` detection, `.gitignore` respect: Task 4 (synthesis + ls-files filter) + Task 8 (view). ✓
- `spec` newest/named/abs path: Task 2. ✓ `--web` with port selection: Task 3 (port) + Task 9 (server). ✓
- Error handling (missing state, missing spec, `delta` fallback, port exhausted, worktree removed): covered in diff/spec views and port picker tests. Missing-`mothership.yaml` bootstrap error is handled by existing `get_container()` in `src/mship/cli/__init__.py:45-48`. ✓
- Testing: unit tests for all core helpers + Textual `run_test()` Pilot tests for each view. ✓

**Placeholder scan:** None — all code shown inline, all file paths exact.

**Type consistency:**
- `ViewApp.gather()` → `str` used uniformly in status/logs/diff; SpecView overrides `_refresh_content` directly because it drives a Markdown widget, not a Static — documented.
- `collect_worktree_diff() -> WorktreeDiff` with `.combined`/`.files_changed` — matches usage in Task 8.
- `pick_port(start, max_tries, explicit)` signature matches Task 3 tests and Task 9 caller.
- `find_spec(workspace_root, name_or_path)` matches Task 2 tests and Tasks 9 callers.
- `SpecNotFoundError`, `NoFreePortError` exported from their respective modules and imported in Task 9.

**Known deferrals (not spec gaps, explicitly out-of-scope):**
- Event-driven (fsevents/inotify) refresh — spec lists this as post-v1.
- `--no-alt-screen` flag — spec mentions it; can land as a trivial follow-up if needed.
- `view logs --all` multiplexing — dropped when we narrowed to task-log source per spec revision.
