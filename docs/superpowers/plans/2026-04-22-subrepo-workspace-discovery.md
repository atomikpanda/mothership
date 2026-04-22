# Subrepo Workspace Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Subrepo worktrees can discover their workspace via a `.mship-workspace` marker (auto-written at spawn) or a `MSHIP_WORKSPACE` env var; git hooks from non-mship repos stay silent instead of printing "No mothership.yaml found" warnings.

**Architecture:** New `workspace_marker.py` module owns marker read/write + per-worktree git-exclude append. `ConfigLoader.discover` grows a priority chain: env var → marker walk-up → yaml walk-up. `get_container(required: bool = True)` parameter lets hook commands return None silently when no workspace exists. Spawn drops the marker in each worktree and updates the per-worktree git exclude so it doesn't pollute tracked `.gitignore`.

**Tech Stack:** Python 3.14, pytest. No new runtime deps.

**Reference spec:** `docs/superpowers/specs/2026-04-22-subrepo-workspace-discovery-design.md`

---

## File structure

**New files:**
- `src/mship/core/workspace_marker.py` — marker read/write + exclude-file helpers.
- `tests/core/test_workspace_marker.py` — unit tests.

**Modified files:**
- `src/mship/core/config.py` — `ConfigLoader.discover` gets env-var + marker-walk-up arms.
- `tests/core/test_config.py` — new discovery priority tests.
- `src/mship/cli/__init__.py` — `get_container(required: bool = True)` param.
- `src/mship/cli/internal.py` — hook commands use `required=False`.
- `tests/cli/test_internal.py` — new or existing file; add hook-silence tests.
- `src/mship/core/worktree.py` — `WorktreeManager.spawn()` drops marker per worktree.
- `src/mship/cli/worktree.py` — spawn caller passes `workspace_root` derived from `container.config_path().parent`.
- `tests/core/test_worktree.py` — integration tests for spawn writing markers.

**Task ordering rationale:** Task 1 ships the pure module first (no external dependencies). Task 2 uses it to extend discovery. Task 3 adds the `required=False` escape hatch for hooks. Task 4 wires spawn to actually write markers. Task 5 is smoke + PR. Earlier tasks are fully independent of later ones; the chain builds cleanly.

---

## Task 1: `workspace_marker` module

**Files:**
- Create: `src/mship/core/workspace_marker.py`
- Create: `tests/core/test_workspace_marker.py`

**Context:** Pure library. No mship-specific state. Three functions:
- `write_marker(worktree_path, workspace_root)`: write `workspace_root` absolute path as one line to `<worktree_path>/.mship-workspace`.
- `read_marker_from_ancestor(start)`: walk up `start` looking for `.mship-workspace`; if found, validate that the path points to a dir with `mothership.yaml`. Return the workspace root Path, or None.
- `append_to_worktree_exclude(worktree_path, parent_git_dir, slug)`: append `.mship-workspace` to `<parent_git_dir>/worktrees/<slug>/info/exclude` (idempotent). Return True on success, False on any error.

- [ ] **Step 1.1: Write failing tests**

Create `tests/core/test_workspace_marker.py`:

```python
"""Unit tests for the workspace_marker module. See #84."""
from pathlib import Path

import pytest


def _write_yaml(path: Path, name: str = "t") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "mothership.yaml").write_text(f"workspace: {name}\nrepos: {{}}\n")


def test_write_marker_creates_file(tmp_path: Path):
    from mship.core.workspace_marker import write_marker, MARKER_NAME
    worktree = tmp_path / "wt"; worktree.mkdir()
    root = tmp_path / "ws"; _write_yaml(root)
    write_marker(worktree, root)
    marker = worktree / MARKER_NAME
    assert marker.exists()
    assert marker.read_text().strip() == str(root.resolve())


def test_write_marker_overwrites_existing(tmp_path: Path):
    from mship.core.workspace_marker import write_marker, MARKER_NAME
    worktree = tmp_path / "wt"; worktree.mkdir()
    (worktree / MARKER_NAME).write_text("/stale/path\n")
    root = tmp_path / "ws"; _write_yaml(root)
    write_marker(worktree, root)
    assert (worktree / MARKER_NAME).read_text().strip() == str(root.resolve())


def test_read_marker_from_ancestor_immediate(tmp_path: Path):
    from mship.core.workspace_marker import read_marker_from_ancestor, write_marker
    worktree = tmp_path / "wt"; worktree.mkdir()
    root = tmp_path / "ws"; _write_yaml(root)
    write_marker(worktree, root)
    assert read_marker_from_ancestor(worktree) == root.resolve()


def test_read_marker_from_ancestor_walks_up(tmp_path: Path):
    from mship.core.workspace_marker import read_marker_from_ancestor, write_marker
    worktree = tmp_path / "wt"; worktree.mkdir()
    nested = worktree / "a" / "b" / "c"; nested.mkdir(parents=True)
    root = tmp_path / "ws"; _write_yaml(root)
    write_marker(worktree, root)
    assert read_marker_from_ancestor(nested) == root.resolve()


def test_read_marker_returns_none_when_absent(tmp_path: Path):
    from mship.core.workspace_marker import read_marker_from_ancestor
    here = tmp_path / "anywhere"; here.mkdir()
    assert read_marker_from_ancestor(here) is None


def test_read_marker_stale_missing_dir_returns_none(tmp_path: Path):
    """Marker points to a dir that doesn't exist → treated as absent."""
    from mship.core.workspace_marker import read_marker_from_ancestor, MARKER_NAME
    worktree = tmp_path / "wt"; worktree.mkdir()
    (worktree / MARKER_NAME).write_text(str(tmp_path / "does-not-exist"))
    assert read_marker_from_ancestor(worktree) is None


def test_read_marker_stale_no_yaml_returns_none(tmp_path: Path):
    """Marker points to an existing dir that has no mothership.yaml → None."""
    from mship.core.workspace_marker import read_marker_from_ancestor, MARKER_NAME
    worktree = tmp_path / "wt"; worktree.mkdir()
    other = tmp_path / "other-dir"; other.mkdir()
    (worktree / MARKER_NAME).write_text(str(other))
    assert read_marker_from_ancestor(worktree) is None


def test_append_to_worktree_exclude_creates_line(tmp_path: Path):
    from mship.core.workspace_marker import (
        append_to_worktree_exclude, MARKER_NAME,
    )
    parent_git = tmp_path / "parent-git"
    wt_info = parent_git / "worktrees" / "my-slug" / "info"
    wt_info.mkdir(parents=True)
    (wt_info / "exclude").write_text("# existing\n*.pyc\n")
    worktree = tmp_path / "wt"; worktree.mkdir()
    ok = append_to_worktree_exclude(worktree, parent_git, "my-slug")
    assert ok is True
    content = (wt_info / "exclude").read_text()
    assert MARKER_NAME in content
    assert "*.pyc" in content  # existing lines preserved


def test_append_to_worktree_exclude_idempotent(tmp_path: Path):
    from mship.core.workspace_marker import (
        append_to_worktree_exclude, MARKER_NAME,
    )
    parent_git = tmp_path / "parent-git"
    wt_info = parent_git / "worktrees" / "s" / "info"
    wt_info.mkdir(parents=True)
    (wt_info / "exclude").write_text(f"{MARKER_NAME}\n")
    worktree = tmp_path / "wt"; worktree.mkdir()
    ok = append_to_worktree_exclude(worktree, parent_git, "s")
    assert ok is True
    # Line appears exactly once.
    lines = [l for l in (wt_info / "exclude").read_text().splitlines() if l == MARKER_NAME]
    assert len(lines) == 1


def test_append_to_worktree_exclude_missing_dir_returns_false(tmp_path: Path):
    """When parent-git/worktrees/<slug>/info doesn't exist, return False gracefully."""
    from mship.core.workspace_marker import append_to_worktree_exclude
    parent_git = tmp_path / "parent-git"; parent_git.mkdir()
    # Don't create the worktrees/<slug>/info path.
    worktree = tmp_path / "wt"; worktree.mkdir()
    assert append_to_worktree_exclude(worktree, parent_git, "absent-slug") is False
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_workspace_marker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.workspace_marker'`.

- [ ] **Step 1.3: Create the module**

Write `src/mship/core/workspace_marker.py`:

```python
"""Workspace-discovery marker used by subrepo worktrees.

Each `mship spawn` drops a one-line `.mship-workspace` file in every
worktree it creates. The file's content is the absolute path of the dir
containing `mothership.yaml`. When a user (or a git hook) runs `mship`
from inside a subrepo worktree — a path that isn't an ancestor of the
workspace root — `ConfigLoader.discover` consults this marker to resolve
the workspace. See #84.

The marker is excluded from git via the worktree's per-worktree exclude
file (`<parent-repo>/.git/worktrees/<slug>/info/exclude`) so it doesn't
pollute tracked `.gitignore`.

Stale markers (pointing to a missing path or a path without
`mothership.yaml`) return None from `read_marker_from_ancestor`, which
lets `discover` fall through to the usual walk-up — no error, no warning.
"""
from __future__ import annotations

from pathlib import Path


MARKER_NAME = ".mship-workspace"


def write_marker(worktree_path: Path, workspace_root: Path) -> None:
    """Write `<worktree_path>/.mship-workspace` containing the workspace root.

    Always overwrites; one-line path, no trailing metadata.
    """
    worktree_path = Path(worktree_path)
    (worktree_path / MARKER_NAME).write_text(str(Path(workspace_root).resolve()) + "\n")


def read_marker_from_ancestor(start: Path) -> Path | None:
    """Walk up from `start` looking for `.mship-workspace`.

    When found, read the path it points at. If that path exists AND contains
    `mothership.yaml`, return the resolved directory. Otherwise return None
    (stale marker; caller should fall through to another discovery step).
    """
    try:
        current = Path(start).resolve()
    except OSError:
        return None
    while True:
        marker = current / MARKER_NAME
        if marker.is_file():
            try:
                target = Path(marker.read_text().strip())
            except OSError:
                return None
            if target.is_dir() and (target / "mothership.yaml").is_file():
                return target.resolve()
            return None  # stale
        parent = current.parent
        if parent == current:
            return None
        current = parent


def append_to_worktree_exclude(
    worktree_path: Path, parent_git_dir: Path, slug: str,
) -> bool:
    """Append `MARKER_NAME` to `<parent_git_dir>/worktrees/<slug>/info/exclude`.

    Idempotent — does not duplicate an existing entry. Returns True on
    success; False on any OS error (missing dir, permission denied, etc.)
    so the caller can degrade gracefully.
    """
    try:
        info_dir = Path(parent_git_dir) / "worktrees" / slug / "info"
        if not info_dir.is_dir():
            return False
        exclude = info_dir / "exclude"
        existing = exclude.read_text() if exclude.is_file() else ""
        if MARKER_NAME in existing.splitlines():
            return True
        suffix = "" if existing.endswith("\n") or not existing else "\n"
        exclude.write_text(existing + suffix + MARKER_NAME + "\n")
        return True
    except OSError:
        return False
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_workspace_marker.py -v`
Expected: 10 passed.

- [ ] **Step 1.5: Commit**

```bash
git add src/mship/core/workspace_marker.py tests/core/test_workspace_marker.py
git commit -m "feat(core): workspace_marker module (write/read/exclude)"
mship journal "#84: workspace_marker.py with write_marker, read_marker_from_ancestor, append_to_worktree_exclude helpers; 10 unit tests" --action committed
```

---

## Task 2: `ConfigLoader.discover` priority chain

**Files:**
- Modify: `src/mship/core/config.py` — `discover` consults env var + marker before walking up for `mothership.yaml`.
- Modify: `tests/core/test_config.py` — new discovery tests.

**Context:** `discover` today only walks ancestors for `mothership.yaml`. Add two earlier arms:
1. `MSHIP_WORKSPACE` env var — set-and-valid wins; set-but-invalid raises `FileNotFoundError` (fail loud on misconfiguration).
2. Marker walk-up via `read_marker_from_ancestor`.

- [ ] **Step 2.1: Write failing tests**

Append to `tests/core/test_config.py`:

```python
def test_discover_env_var_valid(tmp_path, monkeypatch):
    from mship.core.config import ConfigLoader
    root = tmp_path / "ws"
    root.mkdir()
    (root / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    # Start from an unrelated dir.
    other = tmp_path / "other"; other.mkdir()
    monkeypatch.setenv("MSHIP_WORKSPACE", str(root))
    path = ConfigLoader.discover(other)
    assert path == root / "mothership.yaml"


def test_discover_env_var_invalid_raises(tmp_path, monkeypatch):
    from mship.core.config import ConfigLoader
    import pytest
    monkeypatch.setenv("MSHIP_WORKSPACE", str(tmp_path / "does-not-exist"))
    with pytest.raises(FileNotFoundError) as exc:
        ConfigLoader.discover(tmp_path)
    # Error message should name the env var so the user can act.
    assert "MSHIP_WORKSPACE" in str(exc.value)


def test_discover_marker_precedes_walk_up(tmp_path, monkeypatch):
    """Marker at worktree points to root A; walk-up would find root B.
    Marker wins."""
    from mship.core.config import ConfigLoader
    from mship.core.workspace_marker import write_marker
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)

    root_a = tmp_path / "a-ws"; root_a.mkdir()
    (root_a / "mothership.yaml").write_text("workspace: a\nrepos: {}\n")

    # Worktree lives under root_b, but marker points at root_a.
    root_b = tmp_path / "b-ws"; root_b.mkdir()
    (root_b / "mothership.yaml").write_text("workspace: b\nrepos: {}\n")
    worktree = root_b / "wt"; worktree.mkdir()
    write_marker(worktree, root_a)

    path = ConfigLoader.discover(worktree)
    assert path == root_a / "mothership.yaml"


def test_discover_stale_marker_falls_through_to_walk_up(tmp_path, monkeypatch):
    from mship.core.config import ConfigLoader
    from mship.core.workspace_marker import MARKER_NAME
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)

    root = tmp_path / "ws"; root.mkdir()
    (root / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    worktree = root / "sub"; worktree.mkdir()
    # Marker points to a nonexistent path.
    (worktree / MARKER_NAME).write_text(str(tmp_path / "nope"))

    path = ConfigLoader.discover(worktree)
    assert path == root / "mothership.yaml"  # walk-up found it


def test_discover_walk_up_unchanged_when_no_env_no_marker(tmp_path, monkeypatch):
    """Regression: existing behavior works when env var and marker both absent."""
    from mship.core.config import ConfigLoader
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    root = tmp_path / "ws"; root.mkdir()
    (root / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    nested = root / "a" / "b"; nested.mkdir(parents=True)
    assert ConfigLoader.discover(nested) == root / "mothership.yaml"
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_config.py -v -k "discover_env or discover_marker or discover_stale or discover_walk_up_unchanged"`
Expected: the 4 new tests FAIL (behavior not implemented); the `walk_up_unchanged` regression test may already pass if existing logic is intact.

- [ ] **Step 2.3: Implement the priority chain**

Edit `src/mship/core/config.py`. Replace the current `discover` staticmethod (around lines 205-217) with:

```python
    @staticmethod
    def discover(start: Path) -> Path:
        import os
        from mship.core.workspace_marker import read_marker_from_ancestor

        # 1. MSHIP_WORKSPACE env var — set-and-valid wins; set-but-invalid
        #    raises so misconfiguration fails loud instead of silently
        #    falling through to the walk-up.
        env = os.environ.get("MSHIP_WORKSPACE")
        if env:
            env_root = Path(env).resolve()
            env_yaml = env_root / "mothership.yaml"
            if env_yaml.is_file():
                return env_yaml
            raise FileNotFoundError(
                f"MSHIP_WORKSPACE={env!r} does not contain a mothership.yaml "
                f"(expected {env_yaml})"
            )

        # 2. Marker walk-up — subrepo worktrees get a `.mship-workspace`
        #    pointer from spawn. Stale markers return None silently.
        marker_root = read_marker_from_ancestor(start)
        if marker_root is not None:
            return marker_root / "mothership.yaml"

        # 3. Existing walk-up for mothership.yaml.
        current = Path(start).resolve()
        while True:
            candidate = current / "mothership.yaml"
            if candidate.exists():
                return candidate
            parent = current.parent
            if parent == current:
                raise FileNotFoundError(
                    "No mothership.yaml found in any parent directory"
                )
            current = parent
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_config.py -v`
Expected: all pass (existing + 5 new).

- [ ] **Step 2.5: Run broader `tests/core/` for regressions**

Run: `uv run pytest tests/core/ --ignore=tests/core/view/test_web_port.py -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 2.6: Commit**

```bash
git add src/mship/core/config.py tests/core/test_config.py
git commit -m "feat(config): ConfigLoader.discover honors MSHIP_WORKSPACE + .mship-workspace marker"
mship journal "#84: discover priority: env var → marker walk-up → yaml walk-up; invalid MSHIP_WORKSPACE raises loud; stale markers fall through silently" --action committed
```

---

## Task 3: `get_container(required=False)` + hook command silence

**Files:**
- Modify: `src/mship/cli/__init__.py` — `get_container` takes `required: bool = True`; returns None on not-found when `required=False`.
- Modify: `src/mship/cli/internal.py` — three hook commands pass `required=False`.
- Modify: `tests/cli/test_internal.py` (create if missing) — hook-silence tests.

**Context:** The `print("Error: No mothership.yaml found...")` in `get_container` fires from hook invocations even though the hook commands already catch the resulting `typer.Exit`. The stderr print is the real noise source. `required=False` returns None instead of printing+exiting.

- [ ] **Step 3.1: Write failing tests**

Check if `tests/cli/test_internal.py` exists. If not, create it. Append:

```python
"""Tests for hidden _check-commit / _post-checkout / _journal-commit commands."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container


runner = CliRunner()


def test_get_container_required_false_returns_none_when_no_workspace(tmp_path, monkeypatch, capsys):
    """Outside any workspace, get_container(required=False) must be silent.
    See #86."""
    from mship.cli import get_container
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    # Clear any module-level container overrides from previous tests.
    container.config_path.reset_override()
    container.state_dir.reset_override()
    result = get_container(required=False)
    captured = capsys.readouterr()
    assert result is None
    assert captured.err == ""  # no "No mothership.yaml found" noise
    assert captured.out == ""


def test_get_container_required_true_still_errors_loudly(tmp_path, monkeypatch, capsys):
    """Regression: default behavior unchanged — prints + raises."""
    import typer
    from mship.cli import get_container
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    with pytest.raises(typer.Exit) as exc:
        get_container()  # required=True by default
    captured = capsys.readouterr()
    assert exc.value.exit_code == 1
    assert "No mothership.yaml" in captured.err


def test_check_commit_silent_outside_workspace(tmp_path, monkeypatch):
    """_check-commit in a dir with no workspace ancestor exits 0 silently.
    See #86."""
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    # Invoke the command with a toplevel in a non-workspace dir.
    result = runner.invoke(app, ["_check-commit", str(tmp_path)])
    assert result.exit_code == 0
    # No "No mothership.yaml" warning in stderr.
    assert "No mothership.yaml" not in (result.output or "")


def test_journal_commit_silent_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("MSHIP_WORKSPACE", raising=False)
    container.config_path.reset_override()
    container.state_dir.reset_override()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["_journal-commit"])
    assert result.exit_code == 0
    assert "No mothership.yaml" not in (result.output or "")
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_internal.py -v`
Expected: the 4 tests fail — `get_container(required=False)` TypeError (unexpected arg), and hook commands print the warning because they still propagate the error.

- [ ] **Step 3.3: Update `get_container`**

Edit `src/mship/cli/__init__.py`. Find:

```python
def get_container() -> Container:
    """Lazy container initialization with config discovery."""
    from pathlib import Path
    from mship.core.config import ConfigLoader

    try:
        if not container.config_path.overridden:
            config_path = ConfigLoader.discover(Path.cwd())
            container.config_path.override(config_path)
        if not container.state_dir.overridden:
            config_path = container.config_path()
            state_dir = _resolve_state_dir(config_path)
            container.state_dir.override(state_dir)
    except FileNotFoundError:
        import sys
        print("Error: No mothership.yaml found in any parent directory", file=sys.stderr)
        raise typer.Exit(code=1)
    return container
```

Replace with:

```python
def get_container(required: bool = True):
    """Lazy container initialization with config discovery.

    `required=True` (default): missing workspace → stderr error + typer.Exit(1).
    `required=False`: missing workspace → return None silently. Used by hook
    commands so they don't spam `No mothership.yaml` warnings from commits
    in non-mship repos. See #86.
    """
    from pathlib import Path
    from mship.core.config import ConfigLoader

    try:
        if not container.config_path.overridden:
            config_path = ConfigLoader.discover(Path.cwd())
            container.config_path.override(config_path)
        if not container.state_dir.overridden:
            config_path = container.config_path()
            state_dir = _resolve_state_dir(config_path)
            container.state_dir.override(state_dir)
    except FileNotFoundError:
        if not required:
            return None
        import sys
        print("Error: No mothership.yaml found in any parent directory", file=sys.stderr)
        raise typer.Exit(code=1)
    return container
```

- [ ] **Step 3.4: Update the hook commands**

Edit `src/mship/cli/internal.py`. The three hook commands (`_check-commit` at line ~8, `_post-checkout` at line ~127, `_journal-commit` at line ~193) currently call `container = get_container()` inside a `try/except Exception: raise typer.Exit(0)`. Two patterns to change per command:

**Pattern A — preferred**: call with `required=False` and short-circuit on None.

For each of the three commands, find the `container = get_container()` call (inside its try block) and replace with:

```python
            container = get_container(required=False)
            if container is None:
                raise typer.Exit(code=0)
```

The existing `except Exception: raise typer.Exit(code=0)` around it stays as a belt-and-braces guard against other failures (state-load errors, etc.).

Specifically:
- `_check-commit`: around line 20 — replace `container = get_container()` inside the `try` block.
- `_post-checkout`: around line 138 — same replacement.
- `_journal-commit`: around line 200 (search for `container = get_container()` within that command's body) — same replacement.

Full search-and-replace within `src/mship/cli/internal.py`: change every `container = get_container()` to `container = get_container(required=False)\n            if container is None:\n                raise typer.Exit(code=0)` preserving the existing indentation.

(If `get_container` is called anywhere OTHER than inside these three hook commands in `internal.py`, DO NOT change those. Grep first: `grep -n "get_container()" src/mship/cli/internal.py` — should show exactly 3 matches, one per hook command.)

- [ ] **Step 3.5: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_internal.py -v`
Expected: 4 passed.

- [ ] **Step 3.6: Run broader `tests/cli/` for regressions**

Run: `uv run pytest tests/cli/ -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 3.7: Commit**

```bash
git add src/mship/cli/__init__.py src/mship/cli/internal.py tests/cli/test_internal.py
git commit -m "feat(cli): get_container(required=False); hooks silent outside workspace"
mship journal "#86: get_container gains required param; 3 hook commands pass required=False; no more 'No mothership.yaml' spam from commits in non-mship repos" --action committed
```

---

## Task 4: Spawn writes marker + updates per-worktree exclude

**Files:**
- Modify: `src/mship/core/worktree.py` — `WorktreeManager.spawn` accepts a `workspace_root: Path` param (optional-nullable for test ergonomics); writes marker and updates per-worktree exclude for each spawned worktree.
- Modify: `src/mship/cli/worktree.py` — spawn caller passes `container.config_path().parent` as `workspace_root`.
- Modify: `tests/core/test_worktree.py` — integration tests.

**Context:** Spawn currently creates worktrees but doesn't know the workspace root. Pass it explicitly from the CLI caller (which has access via `container.config_path()`).

- [ ] **Step 4.1: Write failing tests**

Append to `tests/core/test_worktree.py`:

```python
def test_spawn_writes_workspace_marker_in_each_worktree(workspace_with_git: Path):
    """Spawn writes `.mship-workspace` in every worktree it creates. See #84."""
    from mship.cli import container
    from mship.core.workspace_marker import MARKER_NAME

    # Use the real spawn command via the CLI so the wiring passes
    # workspace_root through end-to-end.
    from typer.testing import CliRunner
    from mship.cli import app
    runner = CliRunner()

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    (workspace_with_git / ".mothership").mkdir(exist_ok=True)

    try:
        result = runner.invoke(
            app, ["spawn", "marker test", "--repos", "shared", "--skip-setup"]
        )
        assert result.exit_code == 0, result.output
        wt = workspace_with_git / "shared" / ".worktrees" / "feat" / "marker-test"
        marker = wt / MARKER_NAME
        assert marker.is_file(), list(wt.iterdir()) if wt.is_dir() else "worktree missing"
        assert marker.read_text().strip() == str(workspace_with_git.resolve())
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_spawn_appends_marker_to_worktree_exclude(workspace_with_git: Path):
    """Marker is added to the per-worktree info/exclude so it doesn't pollute
    the tracked `.gitignore`."""
    from mship.cli import container, app
    from mship.core.workspace_marker import MARKER_NAME
    from typer.testing import CliRunner
    runner = CliRunner()

    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    (workspace_with_git / ".mothership").mkdir(exist_ok=True)

    try:
        result = runner.invoke(
            app, ["spawn", "exclude test", "--repos", "shared", "--skip-setup"]
        )
        assert result.exit_code == 0, result.output
        # Walk down parent-repo/.git/worktrees/<slug>/info/exclude
        parent_repo = workspace_with_git / "shared"
        # git_dir for a worktree is parent-repo/.git for a normal repo.
        info_exclude = parent_repo / ".git" / "worktrees" / "feat/exclude-test" / "info" / "exclude"
        # NOTE: branch name slash → worktrees use a flattened slug. git
        # worktree add uses the branch name's final component. Adjust if
        # actual naming differs by reading the dir tree.
        if not info_exclude.is_file():
            # Fall back to whatever worktrees dir got created.
            candidates = list((parent_repo / ".git" / "worktrees").iterdir())
            assert candidates, "no per-worktree state dir created"
            info_exclude = candidates[0] / "info" / "exclude"
        content = info_exclude.read_text()
        assert MARKER_NAME in content
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
```

Note: `workspace_with_git` is an existing fixture in `tests/conftest.py` used by other worktree tests. If its behavior doesn't match the assumption above (e.g., creates bare git repos vs. normal), adjust the test setup to match. Review existing `test_spawn_*` tests in `test_worktree.py` for the idiom.

- [ ] **Step 4.2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_worktree.py -v -k "marker or exclude_test"`
Expected: FAIL — `.mship-workspace` doesn't exist in the spawned worktree.

- [ ] **Step 4.3: Update `WorktreeManager.spawn` signature + body**

Edit `src/mship/core/worktree.py`. Update the `spawn` method signature (currently around line 240) to accept `workspace_root`:

```python
    def spawn(
        self,
        description: str,
        repos: list[str] | None = None,
        skip_setup: bool = False,
        slug: str | None = None,
        workspace_root: Path | None = None,
    ) -> SpawnResult:
```

Import at the top of the file (alongside existing imports):

```python
from mship.core.workspace_marker import (
    append_to_worktree_exclude, write_marker,
)
```

In the spawn body, after each worktree is finalized (both the subdir-repo path and the normal-repo path), add:

```python
            # Drop workspace marker + per-worktree exclude so subrepo worktrees
            # can discover the workspace (#84) and don't pollute .gitignore.
            if workspace_root is not None:
                write_marker(wt_path, workspace_root)
                ok = append_to_worktree_exclude(
                    wt_path, repo_path / ".git", branch.split("/")[-1]
                )
                if not ok:
                    setup_warnings.append(
                        f"{repo_name}: could not add {MARKER_NAME} to "
                        f"per-worktree exclude — add it to .gitignore manually."
                    )
```

Place this:
- For the normal-repo branch (not `git_root` subdir): after the worktree is created and `worktrees[repo_name] = wt_path` is set, before the setup task runs.
- For the git_root subdir branch: do NOT add it — the subdir repo shares the parent's worktree directory, and the parent's own spawn call already wrote the marker there.

Only the normal-repo branch needs the marker-write logic. The git_root subdir branch inherits via the parent's worktree.

Also: inside `append_to_worktree_exclude`, we need the per-worktree slug (git's worktree name). For `.worktrees/feat/<slug>`, the slug is the final segment of `branch`. `branch` looks like `feat/<slug>`. Take `branch.split("/")[-1]` for the slug. Git stores per-worktree state at `<parent>/.git/worktrees/<branch-last-segment>/`.

Also import `MARKER_NAME` if you want to use it in the warning; or hardcode `.mship-workspace`. Prefer the import for consistency.

- [ ] **Step 4.4: Update the CLI caller**

Edit `src/mship/cli/worktree.py`. Find the `spawn` handler (`@app.command()` / `def spawn(...)`). Inside the function body, find the `result = wt_mgr.spawn(description, repos=repo_list, skip_setup=skip_setup, slug=slug)` call (or similar — the exact arg list is what Task 1 of the ergonomics PR added, let grep guide you: `grep -n "wt_mgr.spawn" src/mship/cli/worktree.py`).

Update the call to pass `workspace_root`:

```python
        result = wt_mgr.spawn(
            description, repos=repo_list, skip_setup=skip_setup, slug=slug,
            workspace_root=container.config_path().parent,
        )
```

`container.config_path()` returns the absolute path to `mothership.yaml`. Its `.parent` is the workspace root dir — exactly what `write_marker` wants.

- [ ] **Step 4.5: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_worktree.py -v -k "marker or exclude_test"`
Expected: 2 passed.

- [ ] **Step 4.6: Run broader tests for regressions**

Run: `uv run pytest tests/ --ignore=tests/core/view/test_web_port.py -q 2>&1 | tail -5`
Expected: all pass. If any test's assertion on worktree contents now sees the extra `.mship-workspace` file and fails, update that test to accept it (additive behavior — `.mship-workspace` is a legitimate new artifact).

- [ ] **Step 4.7: Commit**

```bash
git add src/mship/core/worktree.py src/mship/cli/worktree.py tests/core/test_worktree.py
git commit -m "feat(spawn): write .mship-workspace marker in each worktree"
mship journal "#84: spawn drops .mship-workspace pointing to workspace root; adds it to per-worktree info/exclude so subrepo worktrees can discover workspace without polluting .gitignore" --action committed
```

---

## Task 5: Smoke + PR

**Files:**
- None (verification + PR only).

- [ ] **Step 5.1: Reinstall**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/workspace-discovery
uv tool install --reinstall --from . mothership
```

- [ ] **Step 5.2: Smoke the marker discovery**

From the worktree, spawn a scratch task and confirm the marker is written + discovery works:

```bash
# Inside the mship workspace (main checkout), spawn a smoke task.
cd /home/bailey/development/repos/mothership
mship spawn "smoke marker" --repos mothership --skip-setup --slug smoke-marker --force-audit
# The worktree is at /home/bailey/development/repos/mothership/.worktrees/feat/smoke-marker
cat /home/bailey/development/repos/mothership/.worktrees/feat/smoke-marker/.mship-workspace
```

Expected: prints the absolute path of the main mship checkout.

Then from a deeply-nested subdir inside that worktree, run a command:

```bash
mkdir -p /home/bailey/development/repos/mothership/.worktrees/feat/smoke-marker/a/b
cd /home/bailey/development/repos/mothership/.worktrees/feat/smoke-marker/a/b
mship status --task smoke-marker 2>&1 | head -5
```

Expected: resolves normally, breadcrumb shows `smoke-marker`. Without this PR, the same command would work (walking up finds `mothership.yaml`) — but in a workspace where the worktree is in a subrepo elsewhere, this is the scenario the marker addresses. Skip ahead if no multi-repo scratch workspace is available.

- [ ] **Step 5.3: Smoke hook silence**

Create a plain git repo outside any mship workspace and commit to it:

```bash
rm -rf /tmp/hook-smoke && mkdir /tmp/hook-smoke && cd /tmp/hook-smoke
git init -q
git config user.email t@t
git config user.name t
# Install mship's pre-commit hook manually.
cat > .git/hooks/pre-commit <<'EOF'
#!/bin/sh
if command -v mship >/dev/null 2>&1; then
    toplevel="$(git rev-parse --show-toplevel)"
    mship _check-commit "$toplevel" || exit 1
fi
EOF
chmod +x .git/hooks/pre-commit
echo x > file.txt && git add file.txt && git commit -m "smoke" 2>&1 | tail -5
```

Expected output: commit succeeds, NO `Error: No mothership.yaml found` line anywhere.

Cleanup: `rm -rf /tmp/hook-smoke`.

- [ ] **Step 5.4: Smoke env var override**

```bash
cd /tmp
MSHIP_WORKSPACE=/home/bailey/development/repos/mothership mship status 2>&1 | head -3
```

Expected: resolves to a task in the main mothership workspace (or exits cleanly). Without `MSHIP_WORKSPACE` set, the same command from `/tmp` would error.

- [ ] **Step 5.5: Cleanup smoke task**

```bash
cd /home/bailey/development/repos/mothership
mship close smoke-marker -y --abandon --force
```

- [ ] **Step 5.6: Full pytest**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/workspace-discovery
uv run pytest tests/ --ignore=tests/core/view/test_web_port.py 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 5.7: Open the PR**

Write `/tmp/workspace-discovery-body.md`:

```markdown
## Summary

Closes #84 and #86.

Subrepo worktrees can now discover their workspace via a `.mship-workspace` marker dropped at spawn time, or via `MSHIP_WORKSPACE`. Git hooks in non-mship repos are silent.

### Commit 1 — `feat(core): workspace_marker module`
New `src/mship/core/workspace_marker.py` with `write_marker`, `read_marker_from_ancestor`, `append_to_worktree_exclude`. 10 unit tests covering normal + stale cases.

### Commit 2 — `feat(config): ConfigLoader.discover honors MSHIP_WORKSPACE + marker`
`discover` priority: `MSHIP_WORKSPACE` env var → marker walk-up → existing yaml walk-up → `FileNotFoundError`. Invalid env var raises loud (fail-fast on misconfig). Stale markers fall through silently.

### Commit 3 — `feat(cli): get_container(required=False); hooks silent outside workspace`
`get_container` gains `required` param. `required=False` returns None instead of print+exit. The three hook commands (`_check-commit`, `_post-checkout`, `_journal-commit`) use `required=False`; git hooks from non-mship repos no longer print `Error: No mothership.yaml found` noise.

### Commit 4 — `feat(spawn): write .mship-workspace marker in each worktree`
`WorktreeManager.spawn` drops the marker pointing at the workspace root and appends `.mship-workspace` to the per-worktree `info/exclude` so it doesn't leak to tracked `.gitignore`.

## Test plan

- [x] `tests/core/test_workspace_marker.py`: 10 unit tests.
- [x] `tests/core/test_config.py`: 5 new discovery priority tests.
- [x] `tests/cli/test_internal.py`: 4 new tests for hook silence + `required` param.
- [x] `tests/core/test_worktree.py`: 2 new tests for spawn writing marker + updating exclude.
- [x] Full suite green.
- [x] Manual smoke: spawn writes marker; plain git repo outside workspace commits silently.

## Anti-goals

- No topology change (worktrees stay under `<repo>/.worktrees/feat/...`).
- No auto-refresh of stale markers.
- No `--workspace` flag.
- No markers in non-mship repos.
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/workspace-discovery
mship finish --body-file /tmp/workspace-discovery-body.md --title "feat(mship): subrepo workspace discovery + hook silence (#84 #86)"
```

Expected: PR URL returned.

---

## Done when

- [x] `.mship-workspace` marker written by spawn in every worktree, pointing to the workspace root.
- [x] `ConfigLoader.discover` checks `MSHIP_WORKSPACE` then marker before walking up.
- [x] `get_container(required=False)` returns None silently for hooks; default stays loud.
- [x] Per-worktree `info/exclude` contains `.mship-workspace` so it doesn't pollute tracked `.gitignore`.
- [x] 21+ new tests pass (10 marker + 5 config + 4 cli + 2 worktree).
- [x] Full pytest green.
- [x] Manual smoke confirms subrepo discovery + hook silence + env-var override.
