# `mship view diff` File-Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `mship view diff` from a single scroll buffer into a two-pane browser — file tree (left) + per-file diff (right) — with auto-collapse for noisy lockfiles.

**Architecture:** Split `WorktreeDiff.combined` into a list of `FileDiff` records in `core/view/diff_sources.py`. Rewrite `DiffView` around a Textual `Horizontal` split: `Tree` on the left, diff `VerticalScroll` on the right. Selection state + per-session lockfile expansion live on the `DiffView` instance. Base class `ViewApp` keeps providing `--watch`, alt-screen, and the quit/refresh keys; per-pane scroll preservation is reset on file selection changes.

**Tech Stack:** Python 3.12+, Textual (`Tree`, `Horizontal`, `VerticalScroll`, `Static`), rich `Text.from_ansi` for ANSI-aware rendering, existing `delta` fallback.

**Spec:** `docs/superpowers/specs/2026-04-13-view-diff-file-browser-design.md`

---

## File Structure

**Modify:**
- `src/mship/core/view/diff_sources.py` — add `FileDiff`, `_LOCKFILE_NAMES`, `split_diff_by_file`; change `WorktreeDiff` to carry `files: tuple[FileDiff, ...]` with `.combined` as a computed property.
- `src/mship/cli/view/diff.py` — rewrite `DiffView` around the two-pane layout.
- `tests/core/view/test_diff_sources.py` — add unit tests for the new helpers; keep existing `.combined` assertions working.
- `tests/cli/view/test_diff_view.py` — replace existing `DiffView` tests with two-pane tests.

---

## Task 1: Data-model refactor in `diff_sources.py`

**Files:**
- Modify: `src/mship/core/view/diff_sources.py`
- Test: `tests/core/view/test_diff_sources.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/view/test_diff_sources.py`:
```python
from mship.core.view.diff_sources import (
    FileDiff,
    _LOCKFILE_NAMES,
    split_diff_by_file,
)


_SAMPLE_TWO_FILES = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "index abc..def 100644\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    " line\n"
    "+new\n"
    "diff --git a/src/bar.py b/src/bar.py\n"
    "index 111..222 100644\n"
    "--- a/src/bar.py\n"
    "+++ b/src/bar.py\n"
    "@@ -1,2 +1,1 @@\n"
    " keep\n"
    "-dropped\n"
)


def test_split_empty_returns_empty_list():
    assert split_diff_by_file("") == []


def test_split_single_file():
    combined = (
        "diff --git a/foo.txt b/foo.txt\n"
        "--- a/foo.txt\n"
        "+++ b/foo.txt\n"
        "@@ -1,0 +1,1 @@\n"
        "+hi\n"
    )
    (f,) = split_diff_by_file(combined)
    assert f.path == "foo.txt"
    assert f.additions == 1
    assert f.deletions == 0
    assert f.body == combined


def test_split_two_files_roundtrip():
    result = split_diff_by_file(_SAMPLE_TWO_FILES)
    assert [f.path for f in result] == ["src/foo.py", "src/bar.py"]
    assert result[0].additions == 1 and result[0].deletions == 0
    assert result[1].additions == 0 and result[1].deletions == 1
    assert "".join(f.body for f in result) == _SAMPLE_TWO_FILES


def test_split_synthesized_untracked_file():
    combined = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+print('hi')\n"
    )
    (f,) = split_diff_by_file(combined)
    assert f.path == "new.py"
    assert f.additions == 1


def test_split_binary_stub():
    combined = (
        "diff --git a/blob.bin b/blob.bin\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/blob.bin\n"
        "new binary file, 42 bytes\n"
    )
    (f,) = split_diff_by_file(combined)
    assert f.path == "blob.bin"
    assert f.additions == 0
    assert f.deletions == 0


def test_split_malformed_chunk_is_tolerated():
    # Missing +++ line; path falls back to "<unknown>"
    combined = "diff --git a/x b/x\nsomething broken\n"
    (f,) = split_diff_by_file(combined)
    assert f.path == "<unknown>"
    assert f.body == combined


def test_file_is_lockfile_true_for_known_names():
    for name in ["package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                  "poetry.lock", "uv.lock", "Pipfile.lock",
                  "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum"]:
        f = FileDiff(path=f"some/sub/{name}", additions=0, deletions=0, body="")
        assert f.is_lockfile, f"{name} should be a lockfile"


def test_file_is_lockfile_false_for_other_names():
    for name in ["src/foo.py", "README.md", "Taskfile.yml", "mothership.yaml"]:
        f = FileDiff(path=name, additions=1, deletions=0, body="")
        assert not f.is_lockfile


def test_collect_worktree_diff_exposes_files(tmp_path):
    import subprocess
    from mship.core.view.diff_sources import collect_worktree_diff

    def _git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed\n")
    _git("add", ".")
    _git("commit", "-q", "-m", "seed")
    (tmp_path / "seed.txt").write_text("seed\nchanged\n")
    (tmp_path / "added.txt").write_text("added\n")

    wd = collect_worktree_diff(tmp_path)
    paths = sorted(f.path for f in wd.files)
    assert paths == ["added.txt", "seed.txt"]
    # Backward-compat: .combined still produces the full concat.
    assert "+changed" in wd.combined
    assert "+++ b/added.txt" in wd.combined
    assert wd.files_changed == len(wd.files) == 2
```

Note: existing tests in this file already assert on `.combined`. They keep passing because `.combined` becomes a computed property over `.files` — no test edits needed.

- [ ] **Step 2: Run tests, verify new ones fail**

Run: `uv run pytest tests/core/view/test_diff_sources.py -v`
Expected: new tests FAIL with `ImportError: cannot import name 'FileDiff'` (and similar for `split_diff_by_file`, `_LOCKFILE_NAMES`).

- [ ] **Step 3: Implement the data-model refactor**

Replace `src/mship/core/view/diff_sources.py` with:
```python
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


_LOCKFILE_NAMES: frozenset[str] = frozenset({
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
})


@dataclass(frozen=True)
class FileDiff:
    path: str
    additions: int
    deletions: int
    body: str

    @property
    def is_lockfile(self) -> bool:
        return Path(self.path).name in _LOCKFILE_NAMES


@dataclass(frozen=True)
class WorktreeDiff:
    root: Path
    files: tuple[FileDiff, ...]

    @property
    def files_changed(self) -> int:
        return len(self.files)

    @property
    def combined(self) -> str:
        return "".join(f.body for f in self.files)


def _is_binary(content: bytes) -> bool:
    return b"\0" in content[:8000]


def synthesize_untracked_diff(worktree: Path, rel_path: Path) -> str:
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
        cwd=worktree, check=True, capture_output=True,
    )
    raw = result.stdout.decode("utf-8", errors="replace")
    return [Path(p) for p in raw.split("\0") if p]


def _tracked_diff(worktree: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=worktree, check=True, capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


_PATH_FROM_PLUS = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_PATH_FROM_MINUS = re.compile(r"^--- a/(.+)$", re.MULTILINE)
_PATH_FROM_HEADER = re.compile(r"^diff --git a/(\S+) b/\S+", re.MULTILINE)


def _parse_one_chunk(chunk: str) -> FileDiff:
    m = _PATH_FROM_PLUS.search(chunk)
    if m and m.group(1) != "/dev/null":
        path = m.group(1)
    else:
        m = _PATH_FROM_MINUS.search(chunk)
        if m and m.group(1) != "/dev/null":
            path = m.group(1)
        else:
            m = _PATH_FROM_HEADER.search(chunk)
            path = m.group(1) if m else "<unknown>"

    additions = 0
    deletions = 0
    for line in chunk.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return FileDiff(path=path, additions=additions, deletions=deletions, body=chunk)


def split_diff_by_file(combined: str) -> list[FileDiff]:
    """Parse a concatenated 'diff --git' stream into per-file chunks."""
    if not combined:
        return []
    # Use a sentinel so the split preserves the 'diff --git ' prefix on each chunk.
    parts = combined.split("\ndiff --git ")
    chunks: list[str] = []
    first = parts[0]
    if first.startswith("diff --git "):
        chunks.append(first if first.endswith("\n") else first + "\n")
    elif first.strip():
        # Leading content before any 'diff --git' header — unusual; keep as-is.
        chunks.append(first if first.endswith("\n") else first + "\n")
    for rest in parts[1:]:
        chunk = "diff --git " + rest
        if not chunk.endswith("\n"):
            chunk += "\n"
        chunks.append(chunk)
    return [_parse_one_chunk(c) for c in chunks if c.strip()]


def collect_worktree_diff(worktree: Path) -> WorktreeDiff:
    tracked = _tracked_diff(worktree)
    untracked_paths = _list_untracked(worktree)
    synthesized = "".join(synthesize_untracked_diff(worktree, p) for p in untracked_paths)
    combined = tracked + synthesized
    files = tuple(split_diff_by_file(combined))
    return WorktreeDiff(root=worktree, files=files)
```

- [ ] **Step 4: Run the full diff-sources test file**

Run: `uv run pytest tests/core/view/test_diff_sources.py -v`
Expected: PASS for all tests — new ones plus the existing `.combined` assertions (which now read through the computed property).

- [ ] **Step 5: Sanity-check nothing else broke**

Run: `uv run pytest -q`
Expected: all previously-passing tests still pass. If any downstream code reads `WorktreeDiff(...)` by positional args (unlikely since it's only used in `diff.py`), the constructor signature changed — fix at the call site. Current call sites: `collect_worktree_diff` (this file) and `diff.py:42` (reads `.files_changed` and `.combined`, both still present). No call-site changes needed.

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/view/diff_sources.py tests/core/view/test_diff_sources.py
git commit -m "refactor(view): split WorktreeDiff into per-file FileDiff records"
```

---

## Task 2: Rewrite `DiffView` with file-tree + diff panes

**Files:**
- Modify: `src/mship/cli/view/diff.py`
- Test: `tests/cli/view/test_diff_view.py`

- [ ] **Step 1: Write the failing tests**

Replace the body of `tests/cli/view/test_diff_view.py` with:
```python
import os
import subprocess
from pathlib import Path

import pytest

from mship.cli.view.diff import DiffView
from mship.core.view.diff_sources import FileDiff, WorktreeDiff


def _fd(path: str, body: str = "", additions: int = 1, deletions: int = 0) -> FileDiff:
    return FileDiff(path=path, additions=additions, deletions=deletions, body=body)


def _wd(root: Path, files: list[FileDiff]) -> WorktreeDiff:
    return WorktreeDiff(root=root, files=tuple(files))


def _seed(view: DiffView, mapping: dict[Path, list[FileDiff]]) -> None:
    """Inject fake worktree diffs so tests don't shell out to git."""
    view._test_override = {p: _wd(p, files) for p, files in mapping.items()}


@pytest.mark.asyncio
async def test_tree_populated_from_worktrees(tmp_path):
    wa = tmp_path / "a"
    wb = tmp_path / "b"
    view = DiffView(worktree_paths=[wa, wb], use_delta=False, watch=False, interval=1.0)
    _seed(view, {
        wa: [_fd("a.py", "diff --git a/a.py b/a.py\n+++ b/a.py\n+x\n"),
              _fd("b.py", "diff --git a/b.py b/b.py\n+++ b/b.py\n+y\n")],
        wb: [_fd("c.py", "diff --git a/c.py b/c.py\n+++ b/c.py\n+z\n")],
    })
    async with view.run_test() as pilot:
        await pilot.pause()
        labels = view.tree_labels()
        assert any(str(wa) in l for l in labels)
        assert any(str(wb) in l for l in labels)
        assert any("a.py" in l for l in labels)
        assert any("c.py" in l for l in labels)


@pytest.mark.asyncio
async def test_first_file_selected_on_mount(tmp_path):
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=1.0)
    _seed(view, {wa: [_fd("first.py", "diff --git a/first.py b/first.py\n+++ b/first.py\n+one\n")]})
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "one" in view.diff_text()


@pytest.mark.asyncio
async def test_large_worktree_starts_collapsed(tmp_path):
    wa = tmp_path / "a"
    wb = tmp_path / "b"
    view = DiffView(worktree_paths=[wa, wb], use_delta=False, watch=False, interval=1.0)
    many = [_fd(f"f{i}.py", f"diff --git a/f{i}.py b/f{i}.py\n+++ b/f{i}.py\n+x\n") for i in range(25)]
    few = [_fd("only.py", "diff --git a/only.py b/only.py\n+++ b/only.py\n+x\n")]
    _seed(view, {wa: many, wb: few})
    async with view.run_test() as pilot:
        await pilot.pause()
        assert view.is_worktree_collapsed(wa) is True
        assert view.is_worktree_collapsed(wb) is False


@pytest.mark.asyncio
async def test_lockfile_collapsed_by_default(tmp_path):
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=1.0)
    lock_body = ("diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n"
                 "+++ b/pnpm-lock.yaml\n"
                 "+noisy noisy noisy\n")
    _seed(view, {wa: [_fd("pnpm-lock.yaml", lock_body, additions=1)]})
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "collapsed" in view.diff_text()
        assert "noisy noisy noisy" not in view.diff_text()
        await pilot.press("e")
        await pilot.pause()
        assert "noisy noisy noisy" in view.diff_text()
        await pilot.press("e")
        await pilot.pause()
        assert "noisy noisy noisy" not in view.diff_text()


@pytest.mark.asyncio
async def test_selection_change_resets_scroll(tmp_path):
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=1.0)
    long_body = "diff --git a/big.py b/big.py\n+++ b/big.py\n" + "".join(
        f"+line {i}\n" for i in range(200)
    )
    _seed(view, {
        wa: [
            _fd("big.py", long_body, additions=200),
            _fd("small.py", "diff --git a/small.py b/small.py\n+++ b/small.py\n+one\n"),
        ],
    })
    async with view.run_test() as pilot:
        await pilot.pause()
        # big.py is selected first; scroll the diff pane down
        view.scroll_diff_to(20)
        assert view.diff_scroll_y() > 0
        view.select_file(wa, "small.py")
        await pilot.pause()
        assert view.diff_scroll_y() == 0


@pytest.mark.asyncio
async def test_refresh_preserves_selection_when_possible(tmp_path):
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=0.05)
    _seed(view, {
        wa: [
            _fd("a.py", "diff --git a/a.py b/a.py\n+++ b/a.py\n+one\n"),
            _fd("b.py", "diff --git a/b.py b/b.py\n+++ b/b.py\n+two\n"),
        ],
    })
    async with view.run_test() as pilot:
        await pilot.pause()
        view.select_file(wa, "b.py")
        await pilot.pause()
        assert "two" in view.diff_text()
        # Same files present on refresh — selection preserved.
        view._refresh_content()
        await pilot.pause()
        assert "two" in view.diff_text()
        # b.py removed — falls back to first available.
        _seed(view, {wa: [_fd("a.py", "diff --git a/a.py b/a.py\n+++ b/a.py\n+one\n")]})
        view._refresh_content()
        await pilot.pause()
        assert "one" in view.diff_text()


@pytest.mark.asyncio
async def test_empty_tree_shows_no_changes(tmp_path):
    wa = tmp_path / "a"
    view = DiffView(worktree_paths=[wa], use_delta=False, watch=False, interval=1.0)
    _seed(view, {wa: []})
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "No changes" in view.diff_text()
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/cli/view/test_diff_view.py -v`
Expected: FAIL — most helpers (`tree_labels`, `diff_text`, `is_worktree_collapsed`, `select_file`, etc.) don't exist yet, and the current view still renders as a single Static.

- [ ] **Step 3: Rewrite `DiffView`**

Replace `src/mship/cli/view/diff.py` with:
```python
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Iterable

import typer
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static, Tree

from mship.cli.view._base import ViewApp
from mship.core.view.diff_sources import FileDiff, WorktreeDiff, collect_worktree_diff


_LARGE_WORKTREE_THRESHOLD = 20


class DiffView(ViewApp):
    BINDINGS = ViewApp.BINDINGS + [
        Binding("e", "toggle_lockfile", "Expand lockfile", show=True),
        Binding("z", "toggle_worktree", "Toggle worktree", show=True),
        Binding("h", "focus_tree", "Focus tree", show=False),
        Binding("l", "focus_diff", "Focus diff", show=False),
    ]

    def __init__(
        self,
        worktree_paths: Iterable[Path],
        use_delta: bool | None = None,
        **kw,
    ):
        super().__init__(**kw)
        self._paths = list(worktree_paths)
        if use_delta is None:
            use_delta = shutil.which("delta") is not None
        self._use_delta = use_delta

        # Populated in _refresh_content
        self._worktrees: dict[Path, WorktreeDiff | Exception] = {}
        self._selected: tuple[Path, str] | tuple[Path, None] | None = None
        self._expanded_lockfiles: set[tuple[Path, str]] = set()
        self._collapsed_worktrees: set[Path] = set()
        self._ever_mounted: set[Path] = set()

        # Test-only hook: if set, skips collect_worktree_diff and uses the
        # provided mapping directly. Must be a dict[Path, WorktreeDiff].
        self._test_override: dict[Path, WorktreeDiff] | None = None

        # Widget refs (populated in compose)
        self._tree: Tree | None = None
        self._diff_static: Static | None = None
        self._diff_scroll: VerticalScroll | None = None

    # --- Textual lifecycle ---
    def compose(self) -> ComposeResult:
        self._tree = Tree("diff", id="diff-tree")
        self._tree.root.expand()
        self._tree.show_root = False
        self._diff_static = Static("", expand=True)
        self._diff_scroll = VerticalScroll(self._diff_static)
        yield Horizontal(self._tree, self._diff_scroll)

    def on_mount(self) -> None:
        self._refresh_content()
        if self._watch:
            self.set_interval(self._interval, self._refresh_content)

    # --- Data loading ---
    def _load_worktrees(self) -> dict[Path, WorktreeDiff | Exception]:
        if self._test_override is not None:
            return dict(self._test_override)
        out: dict[Path, WorktreeDiff | Exception] = {}
        for p in self._paths:
            try:
                out[p] = collect_worktree_diff(p)
            except Exception as e:  # noqa: BLE001 — view must stay alive
                out[p] = e
        return out

    # --- Refresh ---
    def _refresh_content(self) -> None:
        self._worktrees = self._load_worktrees()
        self._rebuild_tree()
        self._render_selected()

    def _rebuild_tree(self) -> None:
        assert self._tree is not None
        self._tree.clear()
        root = self._tree.root

        for p in self._paths:
            wd = self._worktrees.get(p)
            if isinstance(wd, Exception):
                root.add_leaf(f"▶ {p}  (error: {wd})", data=("err", p))
                continue
            if wd is None:
                continue
            if not wd.files:
                continue
            add_total = sum(f.additions for f in wd.files)
            del_total = sum(f.deletions for f in wd.files)
            label = f"▶ {p}  ·  {len(wd.files)} files  ·  +{add_total} -{del_total}"
            node = root.add(label, data=("wt", p))
            for f in wd.files:
                suffix = "(binary)" if "new binary file" in f.body else f"+{f.additions} -{f.deletions}"
                node.add_leaf(f"{f.path}  {suffix}", data=("file", p, f.path))

            # First-time auto-collapse when large; honour user's toggle afterward.
            if p not in self._ever_mounted:
                self._ever_mounted.add(p)
                if len(wd.files) > _LARGE_WORKTREE_THRESHOLD:
                    self._collapsed_worktrees.add(p)

            if p in self._collapsed_worktrees:
                node.collapse()
            else:
                node.expand()

        # Selection preservation
        prev = self._selected
        self._selected = None
        if prev is not None and len(prev) == 3 and prev[0] is not None:
            # File selection
            wd = self._worktrees.get(prev[0])
            if isinstance(wd, WorktreeDiff) and any(f.path == prev[1] for f in wd.files):
                self._selected = (prev[0], prev[1])
        if self._selected is None:
            # Pick first available file in declared path order
            for p in self._paths:
                wd = self._worktrees.get(p)
                if isinstance(wd, WorktreeDiff) and wd.files:
                    self._selected = (p, wd.files[0].path)
                    break

    def _render_selected(self) -> None:
        assert self._diff_static is not None and self._diff_scroll is not None
        if self._selected is None:
            self._diff_static.update(Text("No changes.", justify="center"))
            return
        worktree, file_path = self._selected
        wd = self._worktrees.get(worktree)
        if not isinstance(wd, WorktreeDiff):
            self._diff_static.update(Text(f"error loading {worktree}"))
            return
        file = next((f for f in wd.files if f.path == file_path), None)
        if file is None:
            self._diff_static.update(Text("No changes."))
            return

        key = (worktree, file_path)
        if file.is_lockfile and key not in self._expanded_lockfiles:
            placeholder = (
                f"{file.path}: +{file.additions} -{file.deletions} "
                f"(collapsed — press e to expand)"
            )
            self._diff_static.update(Text(placeholder))
        else:
            rendered = self._render_body(file.body)
            self._diff_static.update(Text.from_ansi(rendered))
        self._diff_scroll.scroll_to(y=0, animate=False)

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

    # --- Tree interaction ---
    def on_tree_node_selected(self, event) -> None:  # Textual sends Tree.NodeSelected
        data = event.node.data
        if not data:
            return
        if data[0] == "file":
            _, worktree, path = data
            self._selected = (worktree, path)
            self._render_selected()
        elif data[0] == "wt":
            _, worktree = data
            if event.node.is_expanded:
                self._collapsed_worktrees.discard(worktree)
            else:
                self._collapsed_worktrees.add(worktree)

    # --- Actions ---
    def action_toggle_lockfile(self) -> None:
        if self._selected is None:
            return
        worktree, file_path = self._selected
        wd = self._worktrees.get(worktree)
        if not isinstance(wd, WorktreeDiff):
            return
        file = next((f for f in wd.files if f.path == file_path), None)
        if file is None or not file.is_lockfile:
            return
        key = (worktree, file_path)
        if key in self._expanded_lockfiles:
            self._expanded_lockfiles.discard(key)
        else:
            self._expanded_lockfiles.add(key)
        self._render_selected()

    def action_toggle_worktree(self) -> None:
        assert self._tree is not None
        node = self._tree.cursor_node
        if node is None:
            return
        # Find the ancestor worktree node
        target = node
        while target is not None:
            data = getattr(target, "data", None)
            if data and data[0] == "wt":
                break
            target = target.parent
        if target is None:
            return
        if target.is_expanded:
            target.collapse()
            self._collapsed_worktrees.add(target.data[1])
        else:
            target.expand()
            self._collapsed_worktrees.discard(target.data[1])

    def action_focus_tree(self) -> None:
        if self._tree is not None:
            self._tree.focus()

    def action_focus_diff(self) -> None:
        if self._diff_scroll is not None:
            self._diff_scroll.focus()

    # --- Test helpers ---
    def tree_labels(self) -> list[str]:
        assert self._tree is not None

        def walk(node):
            out = [str(node.label)]
            for c in node.children:
                out.extend(walk(c))
            return out

        return walk(self._tree.root)

    def diff_text(self) -> str:
        assert self._diff_static is not None
        return str(self._diff_static.content)

    def is_worktree_collapsed(self, worktree: Path) -> bool:
        assert self._tree is not None
        for child in self._tree.root.children:
            data = child.data
            if data and data[0] == "wt" and data[1] == worktree:
                return not child.is_expanded
        raise AssertionError(f"worktree {worktree} not in tree")

    def select_file(self, worktree: Path, file_path: str) -> None:
        self._selected = (worktree, file_path)
        self._render_selected()

    def scroll_diff_to(self, y: float) -> None:
        assert self._diff_scroll is not None
        self._diff_scroll.set_scroll(x=None, y=y)

    def diff_scroll_y(self) -> float:
        assert self._diff_scroll is not None
        return self._diff_scroll.scroll_y


def _collect_workspace_worktrees(container) -> list[Path]:
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
        """Live per-worktree git diff, browsable by file."""
        container = get_container()
        view = DiffView(
            worktree_paths=_collect_workspace_worktrees(container),
            watch=watch,
            interval=interval,
        )
        view.run()
```

- [ ] **Step 4: Run the new tests**

Run: `uv run pytest tests/cli/view/test_diff_view.py -v`
Expected: all 7 pass.

- [ ] **Step 5: Full suite regression**

Run: `uv run pytest -q`
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/view/diff.py tests/cli/view/test_diff_view.py
git commit -m "feat(view): two-pane file-browser layout for mship view diff"
```

---

## Self-Review

**Spec coverage:**
- Two-pane layout with `Horizontal` + `Tree` + `VerticalScroll`: Task 2. ✓
- `FileDiff`, `_LOCKFILE_NAMES`, `split_diff_by_file`: Task 1. ✓
- Backward-compat `.combined` property on `WorktreeDiff`: Task 1. ✓
- Auto-collapse worktrees with >20 files: Task 2 `_rebuild_tree` + `_ever_mounted`. ✓
- Lockfile placeholder + `e` toggle: Task 2 `_render_selected` + `action_toggle_lockfile`. ✓
- Selection preserved across refresh; falls back to first file: Task 2 `_rebuild_tree` preservation block. ✓
- Selection-change resets diff scroll: Task 2 `_render_selected` final `scroll_to(y=0)` + `select_file` calling it. ✓
- Keybindings (`j/k`, `h/l`, `Enter`, `z`, `e`): Task 2 `BINDINGS` + actions. Note: `Enter` is handled by Textual's default `Tree` behavior (toggles node / fires `NodeSelected`), which feeds `on_tree_node_selected` for the diff-focus and collapse behaviors.
- Error handling (worktree error → tree leaf with message; empty tree → "No changes."): Task 2 `_rebuild_tree` and `_render_selected`. ✓
- Unit tests for `split_diff_by_file` + `is_lockfile`: Task 1. ✓
- TUI tests for tree populate, first-file-selected, auto-collapse, lockfile toggle, scroll reset, refresh preservation, empty tree: Task 2. ✓

**Placeholder scan:** None. Every step has concrete code or a concrete command.

**Type consistency:**
- `FileDiff(path, additions, deletions, body)` and `WorktreeDiff(root, files)` match across Task 1 source, Task 1 tests, and Task 2 tests/code.
- `DiffView._test_override: dict[Path, WorktreeDiff] | None` matches the test helper seeding that field.
- `split_diff_by_file(combined: str) -> list[FileDiff]` signature matches all call sites (`collect_worktree_diff`, tests).

**Known deferrals (explicit in spec):**
- Configurable lockfile list.
- Word-level highlighting.
- File-tree search.
- Hunk-level collapse.
- Persisted UI state.
