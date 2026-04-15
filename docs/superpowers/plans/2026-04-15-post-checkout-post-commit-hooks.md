# `post-checkout` + `post-commit` Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend mship's hook layer beyond pre-commit with (1) a post-checkout hook that warns early when the agent checks out a branch outside `mship spawn`, and (2) a post-commit hook that auto-appends a structured log entry for each commit in a task worktree.

**Architecture:** Refactor `core/hooks.py` to parameterize hook name + body, then install all three hooks (`pre-commit`, `post-checkout`, `post-commit`) per git root with the same MSHIP-BEGIN/END marker. Two new hidden CLI commands — `_post-checkout` and `_log-commit` — do the real work; the hook scripts are ~5 lines of shell delegating to them. Doctor rolls the three into a single check.

**Tech Stack:** Python 3.12+, Typer, POSIX shell, existing `StateManager` + `LogManager`.

**Spec:** `docs/superpowers/specs/2026-04-15-post-checkout-post-commit-hooks-design.md`

---

## File Structure

**Modify:**
- `src/mship/core/hooks.py` — parameterize hook name + body; install/uninstall/is_installed apply to all three.
- `src/mship/cli/internal.py` — add `_post-checkout` and `_log-commit` commands.
- `src/mship/core/doctor.py` — single hook-presence check verifying all three.
- `tests/core/test_hooks.py` — extend for the three-hook surface.
- `tests/cli/test_check_commit.py` (or sibling) — tests for the two new commands.
- `tests/test_hook_integration.py` — end-to-end tests.

**No new files.**

---

## Task 1: Refactor `hooks.py` to install all three hooks

**Files:**
- Modify: `src/mship/core/hooks.py`
- Test: `tests/core/test_hooks.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_hooks.py`:
```python
def test_install_creates_all_three_hook_files(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    install_hook(tmp_path)
    hooks = tmp_path / ".git" / "hooks"
    assert (hooks / "pre-commit").exists()
    assert (hooks / "post-checkout").exists()
    assert (hooks / "post-commit").exists()
    for name in ("pre-commit", "post-checkout", "post-commit"):
        content = (hooks / name).read_text()
        assert HOOK_MARKER_BEGIN in content
        assert HOOK_MARKER_END in content


def test_each_hook_has_distinct_body(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    install_hook(tmp_path)
    hooks = tmp_path / ".git" / "hooks"
    pre = (hooks / "pre-commit").read_text()
    post_co = (hooks / "post-checkout").read_text()
    post_ci = (hooks / "post-commit").read_text()
    assert "mship _check-commit" in pre
    assert "mship _post-checkout" in post_co
    assert "mship _log-commit" in post_ci


def test_is_installed_requires_all_three(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    install_hook(tmp_path)
    assert is_installed(tmp_path) is True

    # Remove post-checkout — is_installed should now be False
    (tmp_path / ".git" / "hooks" / "post-checkout").unlink()
    assert is_installed(tmp_path) is False


def test_uninstall_strips_all_three(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    hooks = tmp_path / ".git" / "hooks"
    # Seed each hook file with user content first
    for name in ("pre-commit", "post-checkout", "post-commit"):
        (hooks / name).write_text(f"#!/bin/sh\n# user {name}\n")
    install_hook(tmp_path)
    uninstall_hook(tmp_path)
    for name in ("pre-commit", "post-checkout", "post-commit"):
        content = (hooks / name).read_text()
        assert f"user {name}" in content
        assert HOOK_MARKER_BEGIN not in content
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_hooks.py -v -k "all_three or distinct_body or requires_all or strips_all"`
Expected: FAIL — only pre-commit is installed today.

- [ ] **Step 3: Refactor `hooks.py`**

Replace the contents of `src/mship/core/hooks.py` with the generalized version:

```python
"""Install / uninstall / detect mship git hooks.

Each hook is a small POSIX shell block wrapped in MSHIP-BEGIN/END markers so it
coexists with user hooks. We never overwrite foreign content; we append or
strip our block as needed.
"""
from __future__ import annotations

import stat
from pathlib import Path


HOOK_MARKER_BEGIN = "# MSHIP-BEGIN"
HOOK_MARKER_END = "# MSHIP-END"


def _block(body_sh: str) -> str:
    return (
        f"{HOOK_MARKER_BEGIN} — managed by mship; edit outside this block is fine\n"
        f"{body_sh}"
        f"{HOOK_MARKER_END}\n"
    )


_PRE_COMMIT_BODY = """if command -v mship >/dev/null 2>&1; then
    toplevel="$(git rev-parse --show-toplevel)"
    mship _check-commit "$toplevel" || exit 1
fi
"""

_POST_CHECKOUT_BODY = """if command -v mship >/dev/null 2>&1; then
    prev_head="$1"
    new_head="$2"
    is_branch_checkout="$3"
    if [ "$is_branch_checkout" = "1" ]; then
        mship _post-checkout "$prev_head" "$new_head" || true
    fi
fi
"""

_POST_COMMIT_BODY = """if command -v mship >/dev/null 2>&1; then
    mship _log-commit || true
fi
"""


# Public hook inventory — name → (file header comment, body)
_HOOKS: dict[str, tuple[str, str]] = {
    "pre-commit": ("# git pre-commit hook", _PRE_COMMIT_BODY),
    "post-checkout": ("# git post-checkout hook", _POST_CHECKOUT_BODY),
    "post-commit": ("# git post-commit hook", _POST_COMMIT_BODY),
}


def _hook_path(git_root: Path, name: str) -> Path:
    return git_root / ".git" / "hooks" / name


def _install_one(git_root: Path, name: str, header: str, body_sh: str) -> None:
    hooks_dir = git_root / ".git" / "hooks"
    if not hooks_dir.exists():
        raise FileNotFoundError(f"git hooks dir not found: {hooks_dir}")

    path = _hook_path(git_root, name)
    block = _block(body_sh)

    if path.exists():
        content = path.read_text()
        if HOOK_MARKER_BEGIN in content:
            _chmod_executable(path)
            return
        if not content.endswith("\n"):
            content += "\n"
        if not content.endswith("\n\n"):
            content += "\n"
        content += block
        path.write_text(content)
    else:
        path.write_text(f"#!/bin/sh\n{header}\n{block}")

    _chmod_executable(path)


def _uninstall_one(git_root: Path, name: str) -> None:
    path = _hook_path(git_root, name)
    if not path.exists():
        return
    content = path.read_text()
    if HOOK_MARKER_BEGIN not in content:
        return

    begin_idx = content.index(HOOK_MARKER_BEGIN)
    end_search = content.find(HOOK_MARKER_END, begin_idx)
    if end_search == -1:
        return
    after_end = content.find("\n", end_search)
    after_end = len(content) if after_end == -1 else after_end + 1

    cut_start = begin_idx
    if cut_start >= 2 and content[cut_start - 2:cut_start] == "\n\n":
        cut_start -= 1

    new_content = content[:cut_start] + content[after_end:]
    while new_content.endswith("\n\n"):
        new_content = new_content[:-1]
    path.write_text(new_content)


def _one_is_installed(git_root: Path, name: str) -> bool:
    path = _hook_path(git_root, name)
    if not path.exists():
        return False
    return HOOK_MARKER_BEGIN in path.read_text()


# --- Public API ---

def is_installed(git_root: Path) -> bool:
    """True if ALL three hooks contain our marker block."""
    return all(_one_is_installed(git_root, name) for name in _HOOKS)


def install_hook(git_root: Path) -> None:
    """Install pre-commit, post-checkout, and post-commit hooks at `<git_root>/.git/hooks/`.

    Idempotent. Appends to existing user content; never overwrites.
    """
    for name, (header, body) in _HOOKS.items():
        _install_one(git_root, name, header, body)


def uninstall_hook(git_root: Path) -> None:
    """Remove our MSHIP block from all three hook files, preserving user content."""
    for name in _HOOKS:
        _uninstall_one(git_root, name)


def _chmod_executable(path: Path) -> None:
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# Preserved for backward compat with any external caller of the old name.
HOOK_BLOCK = _block(_PRE_COMMIT_BODY)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/core/test_hooks.py -v`
Expected: PASS — all existing tests + the 4 new ones.

Existing tests (`test_install_creates_hook_when_missing`, `test_install_is_idempotent`, `test_install_appends_to_existing_hook`, `test_is_installed_detects_marker`, `test_is_installed_false_when_hook_exists_without_marker`, `test_uninstall_removes_mship_block_preserves_other_content`, `test_uninstall_on_file_without_marker_is_noop`, `test_install_when_git_dir_missing_raises`) pass against pre-commit specifically — the generalization keeps their behavior intact. `test_is_installed_detects_marker` and `test_is_installed_false_when_hook_exists_without_marker` may fail because `is_installed` now requires all three — update them to install all three first, or tighten to probe pre-commit specifically. If they fail, adjust by:

- `test_is_installed_detects_marker`: call `install_hook(tmp_path)` (installs all three) before asserting True.
- `test_is_installed_false_when_hook_exists_without_marker`: the test seeds ONLY a pre-commit file; `is_installed` returns False because post-checkout is missing. Behavior still matches the spirit of the test. The assertion `assert is_installed(tmp_path) is False` remains correct.
- `test_uninstall_on_file_without_marker_is_noop`: seeds only pre-commit without marker; `uninstall_hook` tries all three, each is a no-op (either missing or no marker). Assertion still holds: `assert hook.read_text() == original`.

Run the full test file and fix any that rely on exact file layout — none should need more than a line of adjustment.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/hooks.py tests/core/test_hooks.py
git commit -m "refactor(hooks): install pre-commit, post-checkout, post-commit together"
```

---

## Task 2: `mship _post-checkout` hidden command

**Files:**
- Modify: `src/mship/cli/internal.py`
- Test: `tests/cli/test_internal_hooks.py` (new)

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_internal_hooks.py`:
```python
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState


runner = CliRunner()


def _init_repo_on_branch(path: Path, branch: str) -> None:
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True, capture_output=True)
    (path / "x.txt").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True, capture_output=True, env=env)


def _override(tmp_path: Path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def test_post_checkout_silent_on_default_branch(tmp_path, monkeypatch):
    _init_repo_on_branch(tmp_path, "main")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    _override(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["_post-checkout", "HEAD", "HEAD"])
        assert result.exit_code == 0, result.output
        assert "mship:" not in result.output
    finally:
        _reset()


def test_post_checkout_warns_when_no_active_task(tmp_path, monkeypatch):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    subprocess.run(["git", "checkout", "-qb", "feat/rogue"], cwd=tmp_path,
                   check=True, capture_output=True, env=env)
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    _override(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["_post-checkout", "HEAD", "HEAD"])
        assert result.exit_code == 0, result.output
        assert "mship spawn" in result.output
        assert "feat/rogue" in result.output
    finally:
        _reset()


def test_post_checkout_silent_when_on_task_branch_and_cwd_in_worktree(tmp_path, monkeypatch):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    wt = tmp_path / ".worktrees" / "feat-t"
    subprocess.run(["git", "worktree", "add", "-b", "feat/t", str(wt)],
                   cwd=tmp_path, check=True, capture_output=True, env=env)
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    sm = StateManager(tmp_path / ".mothership")
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], branch="feat/t",
        worktrees={"r": wt},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    _override(tmp_path)
    try:
        monkeypatch.chdir(wt)
        result = runner.invoke(app, ["_post-checkout", "HEAD", "HEAD"])
        assert result.exit_code == 0, result.output
        assert "mship:" not in result.output
    finally:
        _reset()


def test_post_checkout_warns_when_branch_mismatches_task(tmp_path, monkeypatch):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    # Create a different branch outside the task's expected branch
    subprocess.run(["git", "checkout", "-qb", "feat/wrong"], cwd=tmp_path,
                   check=True, capture_output=True, env=env)
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    sm = StateManager(tmp_path / ".mothership")
    task = Task(
        slug="add-labels", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], branch="feat/add-labels",
        worktrees={"r": tmp_path / "fake-wt"},
    )
    sm.save(WorkspaceState(current_task="add-labels", tasks={"add-labels": task}))

    _override(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["_post-checkout", "HEAD", "HEAD"])
        assert result.exit_code == 0, result.output
        assert "add-labels" in result.output
        assert "feat/add-labels" in result.output
        assert "feat/wrong" in result.output
    finally:
        _reset()


def test_post_checkout_warns_when_on_task_branch_but_not_in_worktree(tmp_path, monkeypatch):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    # Check out the task branch in the MAIN checkout (not the worktree)
    subprocess.run(["git", "checkout", "-qb", "feat/t"], cwd=tmp_path,
                   check=True, capture_output=True, env=env)
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    sm = StateManager(tmp_path / ".mothership")
    expected_wt = tmp_path / ".worktrees" / "feat-t"
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], branch="feat/t",
        worktrees={"r": expected_wt},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    _override(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)  # NOT the worktree
        result = runner.invoke(app, ["_post-checkout", "HEAD", "HEAD"])
        assert result.exit_code == 0, result.output
        assert str(expected_wt) in result.output
        assert "cd" in result.output.lower()
    finally:
        _reset()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/test_internal_hooks.py -v`
Expected: FAIL — `_post-checkout` command not registered.

- [ ] **Step 3: Implement `_post-checkout`**

In `src/mship/cli/internal.py`, add below the existing `_check-commit` command:

```python
    @app.command(name="_post-checkout", hidden=True)
    def post_checkout(
        prev_head: str = typer.Argument(..., help="git $1 — previous HEAD"),
        new_head: str = typer.Argument(..., help="git $2 — new HEAD"),
    ):
        """Warn loudly when the agent checks out a branch outside mship's expected flow."""
        import subprocess
        from pathlib import Path

        try:
            container = get_container()
            state = container.state_manager().load()
        except Exception:
            raise typer.Exit(code=0)

        # Current branch (after checkout)
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

        import sys
        if state.current_task is None:
            sys.stderr.write(
                f"\u26a0 mship: checked out '{current_branch}' but no active mship task.\n"
                f"  If you're starting feature work, run `mship spawn \"<description>\"` — "
                f"it'll give you a proper worktree and state.\n"
            )
            raise typer.Exit(code=0)

        task = state.tasks.get(state.current_task)
        if task is None:
            raise typer.Exit(code=0)

        cwd = Path.cwd().resolve()
        in_worktree = any(
            cwd == Path(p).resolve() or cwd.is_relative_to(Path(p).resolve())
            for p in task.worktrees.values()
        )

        if current_branch == task.branch and in_worktree:
            raise typer.Exit(code=0)

        if current_branch != task.branch:
            sys.stderr.write(
                f"\u26a0 mship: checked out '{current_branch}' but active task "
                f"'{task.slug}' is on '{task.branch}'.\n"
                f"  If this was a mistake, `git checkout {task.branch}` in the worktree.\n"
                f"  If you're switching tasks, run `mship close --abandon` first.\n"
            )
            raise typer.Exit(code=0)

        # current_branch == task.branch but cwd isn't in a worktree
        worktree_paths = [str(Path(p).resolve()) for p in task.worktrees.values()]
        primary = worktree_paths[0] if worktree_paths else ""
        sys.stderr.write(
            f"\u26a0 mship: you checked out '{current_branch}' here, but the task's worktree is\n"
            f"  {primary}\n"
            f"  cd there — don't edit in main.\n"
        )
        raise typer.Exit(code=0)
```

Note: `Path.is_relative_to` was added in Python 3.9. Confirmed available.

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/cli/test_internal_hooks.py -v -k "post_checkout"`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/internal.py tests/cli/test_internal_hooks.py
git commit -m "feat(cli): add mship _post-checkout internal command for post-checkout hook"
```

---

## Task 3: `mship _log-commit` hidden command

**Files:**
- Modify: `src/mship/cli/internal.py`
- Test: `tests/cli/test_internal_hooks.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_internal_hooks.py`:
```python
def test_log_commit_appends_entry_when_in_task_worktree(tmp_path, monkeypatch):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    wt = tmp_path / ".worktrees" / "feat-t"
    subprocess.run(["git", "worktree", "add", "-b", "feat/t", str(wt)],
                   cwd=tmp_path, check=True, capture_output=True, env=env)
    # Make a commit in the worktree so `git log -1` has something to read
    (wt / "file.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "test commit subject"],
                   cwd=wt, check=True, capture_output=True, env=env)

    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    sm = StateManager(tmp_path / ".mothership")
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], branch="feat/t",
        worktrees={"r": wt},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    _override(tmp_path)
    try:
        monkeypatch.chdir(wt)
        result = runner.invoke(app, ["_log-commit"])
        assert result.exit_code == 0, result.output

        from mship.core.log import LogManager
        entries = LogManager(tmp_path / ".mothership" / "logs").read("t")
        auto = [e for e in entries if e.action == "committed"]
        assert auto, [e.message for e in entries]
        assert "test commit subject" in auto[-1].message
        assert auto[-1].repo == "r"
    finally:
        _reset()


def test_log_commit_silent_when_no_active_task(tmp_path, monkeypatch):
    _init_repo_on_branch(tmp_path, "main")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    _override(tmp_path)
    try:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["_log-commit"])
        assert result.exit_code == 0, result.output
    finally:
        _reset()


def test_log_commit_silent_when_cwd_not_in_worktree(tmp_path, monkeypatch):
    """--no-verify case: commit happens outside any worktree; don't log."""
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _init_repo_on_branch(tmp_path, "main")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    sm = StateManager(tmp_path / ".mothership")
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["r"], branch="feat/t",
        worktrees={"r": tmp_path / ".worktrees" / "feat-t"},
    )
    sm.save(WorkspaceState(current_task="t", tasks={"t": task}))

    _override(tmp_path)
    try:
        # cwd = main checkout, not the worktree
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["_log-commit"])
        assert result.exit_code == 0, result.output

        from mship.core.log import LogManager
        entries = LogManager(tmp_path / ".mothership" / "logs").read("t")
        assert not any(e.action == "committed" for e in entries)
    finally:
        _reset()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/test_internal_hooks.py -v -k "log_commit"`
Expected: FAIL — `_log-commit` command missing.

- [ ] **Step 3: Implement `_log-commit`**

In `src/mship/cli/internal.py`, add below `_post-checkout`:

```python
    @app.command(name="_log-commit", hidden=True)
    def log_commit():
        """Auto-append a structured log entry for the just-made commit.

        Silent no-op when no active task, no mship workspace, or cwd isn't inside
        any known worktree (e.g. `--no-verify` committed somewhere unexpected).
        """
        import subprocess
        from pathlib import Path

        try:
            container = get_container()
            state = container.state_manager().load()
        except Exception:
            raise typer.Exit(code=0)

        if state.current_task is None:
            raise typer.Exit(code=0)

        task = state.tasks.get(state.current_task)
        if task is None or not task.worktrees:
            raise typer.Exit(code=0)

        cwd = Path.cwd().resolve()
        matched_repo: str | None = None
        for repo_name, wt_path in task.worktrees.items():
            wt_resolved = Path(wt_path).resolve()
            if cwd == wt_resolved or (
                cwd.is_relative_to(wt_resolved) if hasattr(Path, "is_relative_to") else False
            ):
                matched_repo = repo_name
                break
        if matched_repo is None:
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
                task.slug,
                f"commit {sha[:10]}: {subject}",
                repo=matched_repo,
                iteration=task.test_iteration if task.test_iteration else None,
                action="committed",
            )
        except Exception:
            pass
        raise typer.Exit(code=0)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/cli/test_internal_hooks.py -v`
Expected: PASS (all 8 tests in the file).

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/internal.py tests/cli/test_internal_hooks.py
git commit -m "feat(cli): add mship _log-commit internal command for post-commit hook"
```

---

## Task 4: Doctor adjusts hook check to cover all three

**Files:**
- Modify: `src/mship/core/doctor.py`
- Test: `tests/core/test_doctor.py`

- [ ] **Step 1: Update the failing-test expectations**

The existing `test_doctor_warns_when_hook_missing` and sibling tests already pass because `is_installed` now requires all three hooks — a fresh workspace has none, so `is_installed` returns False, so doctor warns. Good.

Add a new test to verify partial installation is also caught:

Append to `tests/core/test_doctor.py`:
```python
def test_doctor_warns_when_only_some_hooks_installed(tmp_path):
    """If pre-commit is installed but post-checkout is missing, doctor should warn."""
    import subprocess
    from mship.core.config import ConfigLoader
    from mship.core.doctor import DoctorChecker
    from mship.core.hooks import install_hook
    from mship.util.shell import ShellRunner

    repo = tmp_path / "cli"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    install_hook(repo)
    # Now delete one of the three to simulate partial state
    (repo / ".git" / "hooks" / "post-checkout").unlink()

    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  cli:\n    path: ./cli\n    type: service\n"
    )
    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    report = DoctorChecker(cfg, ShellRunner()).run()
    hook_checks = [c for c in report.checks if "hook" in c.name.lower()]
    assert any(c.status == "warn" for c in hook_checks)
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/core/test_doctor.py -v -k "only_some_hooks"`

It may actually pass already because `is_installed` now checks all three. Run it; if it passes already, proceed to Step 4 (nothing to implement in doctor; `is_installed`'s tightened semantics already cover this case).

- [ ] **Step 3: Update the doctor message (if tests reveal nothing, skip)**

In `src/mship/core/doctor.py`, update the warning message for the hook check to reflect that all three hooks are expected. Find the existing block that reads roughly:

```python
report.checks.append(CheckResult(
    name=hook_name, status="warn",
    message=(
        f"pre-commit hook missing at {root}/.git/hooks/pre-commit. "
        f"Run `mship init --install-hooks` to install."
    ),
))
```

Replace with:

```python
report.checks.append(CheckResult(
    name=hook_name, status="warn",
    message=(
        f"git hooks missing or incomplete at {root}/.git/hooks/. "
        f"Expected mship blocks in pre-commit, post-checkout, post-commit. "
        f"Run `mship init --install-hooks` to install."
    ),
))
```

And for the pass message:

```python
report.checks.append(CheckResult(
    name=hook_name, status="pass",
    message=f"mship git hooks installed at {root}/.git/hooks/",
))
```

- [ ] **Step 4: Run the doctor tests**

Run: `uv run pytest tests/core/test_doctor.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/doctor.py tests/core/test_doctor.py
git commit -m "feat(doctor): cover all three mship hooks in the hook-presence check"
```

---

## Task 5: Integration tests for end-to-end hook behavior

**Files:**
- Modify: `tests/test_hook_integration.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hook_integration.py`:
```python
def test_init_installs_all_three_hooks(workspace_for_hooks):
    tmp_path, repo = workspace_for_hooks
    result = runner.invoke(app, ["init", "--install-hooks"])
    assert result.exit_code == 0, result.output
    hooks = repo / ".git" / "hooks"
    assert (hooks / "pre-commit").exists()
    assert (hooks / "post-checkout").exists()
    assert (hooks / "post-commit").exists()


def test_post_checkout_warns_on_rogue_branch(workspace_for_hooks):
    """git checkout -b outside mship spawn fires a stderr warning."""
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    result = subprocess.run(
        ["git", "checkout", "-b", "feat/rogue"],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    # Checkout itself succeeds — post-checkout only warns
    assert result.returncode == 0
    assert "mship spawn" in result.stderr


def test_post_commit_auto_logs_in_worktree(workspace_for_hooks):
    """A commit inside a task worktree triggers an auto-log entry."""
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])
    spawn_result = runner.invoke(
        app, ["spawn", "auto log test", "--repos", "cli", "--force-audit", "--skip-setup"],
    )
    assert spawn_result.exit_code == 0, spawn_result.output

    from mship.core.state import StateManager
    state = StateManager(tmp_path / ".mothership").load()
    wt = Path(state.tasks["auto-log-test"].worktrees["cli"])
    assert wt.exists()

    (wt / "file.txt").write_text("hello\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "."], cwd=wt, check=True, capture_output=True, env=env)
    result = subprocess.run(
        ["git", "commit", "-m", "auto logged"],
        cwd=wt, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    from mship.core.log import LogManager
    entries = LogManager(tmp_path / ".mothership" / "logs").read("auto-log-test")
    auto = [e for e in entries if e.action == "committed"]
    assert auto
    assert "auto logged" in auto[-1].message
    assert auto[-1].repo == "cli"
```

- [ ] **Step 2: Run tests, verify they pass**

Run: `uv run pytest tests/test_hook_integration.py -v`
Expected: PASS — all three new tests plus the five existing ones.

Then: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 3: Commit**

```bash
git add tests/test_hook_integration.py
git commit -m "test(hooks): end-to-end post-checkout warning and post-commit auto-log"
```

---

## Self-Review

**Spec coverage:**
- post-checkout hook installed + warns in each of the 4 branches: Task 1 (install) + Task 2 (logic + unit tests) + Task 5 (integration).
- post-commit hook installed + auto-logs: Task 1 (install) + Task 3 (logic + unit tests) + Task 5 (integration).
- `install_hook` installs all three: Task 1.
- `is_installed`/`uninstall_hook` cover all three: Task 1.
- Doctor covers all three: Task 4.
- `mship init --install-hooks` installs all three automatically: no changes needed — it already calls `install_hook`, which now installs all three.
- MSHIP-BEGIN/END marker preserved on each hook type: Task 1 template.
- `--no-verify` commits don't auto-log (covered by "cwd not in worktree" check): Task 3.
- `git switch` also triggers post-checkout (git behavior): no code change needed; covered by same hook.

**Placeholder scan:** none — every step has concrete code, concrete test assertions, exact git commands.

**Type consistency:**
- `install_hook(git_root: Path) -> None`, `is_installed(git_root: Path) -> bool`, `uninstall_hook(git_root: Path) -> None` signatures unchanged from prior feature; callers (`init`, `doctor`) unaffected.
- `_post-checkout prev new` and `_log-commit` (no args) match the hook script invocations.
- `LogManager.append(slug, msg, *, repo, iteration, action)` signature matches existing kwargs used elsewhere.

**Known deferrals:** pre-push hook, editor-level intervention, fork of superpowers, `mship check-commit` as an eager spawn reminder. All explicit in spec.
