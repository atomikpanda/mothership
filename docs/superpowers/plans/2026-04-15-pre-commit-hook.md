# Pre-Commit Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a workspace-level pre-commit hook that blocks commits outside the active task's worktrees, installed by `mship init` and kept in sync via `mship doctor`.

**Architecture:** Core module `core/hooks.py` owns hook file I/O (install/uninstall/detect) via a bracketed MSHIP-BEGIN/END marker so it coexists with user hooks. Hidden `mship _check-commit <toplevel>` CLI command reads state and decides allow/deny; the hook is ~5 lines of POSIX shell that delegates to it. `mship init` installs one hook per unique effective git root; `--install-hooks` flag re-runs just the install step. `mship doctor` warns when the hook is missing or lacks the marker.

**Tech Stack:** Python 3.12+, Typer, Pydantic, POSIX shell, existing `StateManager` + `DoctorChecker`.

**Spec:** `docs/superpowers/specs/2026-04-15-pre-commit-hook-design.md`

---

## File Structure

**Create:**
- `src/mship/core/hooks.py` — `install_hook`, `uninstall_hook`, `is_installed`, template constants.
- `src/mship/cli/internal.py` — hidden `_check-commit` command.
- `tests/core/test_hooks.py`.
- `tests/cli/test_check_commit.py`.
- `tests/test_hook_integration.py`.

**Modify:**
- `src/mship/cli/__init__.py` — register internal sub-app.
- `src/mship/cli/init.py` — call installer at end of init; `--install-hooks` flag for standalone re-install.
- `src/mship/core/doctor.py` — per-git-root hook check.
- `tests/core/test_doctor.py` — extend for hook checks.
- `skills/working-with-mothership/SKILL.md`, `README.md` — document hook behavior and `--install-hooks`.

---

## Task 1: `core/hooks.py` — install, uninstall, detect

**Files:**
- Create: `src/mship/core/hooks.py`
- Test: `tests/core/test_hooks.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_hooks.py`:
```python
import os
import stat
from pathlib import Path

import pytest

from mship.core.hooks import (
    HOOK_MARKER_BEGIN, HOOK_MARKER_END,
    install_hook, uninstall_hook, is_installed,
)


def _hook_path(git_root: Path) -> Path:
    return git_root / ".git" / "hooks" / "pre-commit"


def _init_repo(path: Path) -> None:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)


def test_install_creates_hook_when_missing(tmp_path):
    _init_repo(tmp_path)
    install_hook(tmp_path)
    hook = _hook_path(tmp_path)
    assert hook.exists()
    content = hook.read_text()
    assert content.startswith("#!/bin/sh")
    assert HOOK_MARKER_BEGIN in content
    assert HOOK_MARKER_END in content
    # Executable
    mode = hook.stat().st_mode
    assert mode & stat.S_IXUSR


def test_install_is_idempotent(tmp_path):
    _init_repo(tmp_path)
    install_hook(tmp_path)
    first = _hook_path(tmp_path).read_text()
    install_hook(tmp_path)
    second = _hook_path(tmp_path).read_text()
    assert first == second


def test_install_appends_to_existing_hook(tmp_path):
    _init_repo(tmp_path)
    hook = _hook_path(tmp_path)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\n# user hook\necho 'user pre-commit step'\n")
    hook.chmod(0o755)

    install_hook(tmp_path)
    content = hook.read_text()
    assert "user pre-commit step" in content
    assert HOOK_MARKER_BEGIN in content
    assert HOOK_MARKER_END in content
    # MSHIP block appears AFTER the user's content
    assert content.index("user pre-commit step") < content.index(HOOK_MARKER_BEGIN)


def test_is_installed_detects_marker(tmp_path):
    _init_repo(tmp_path)
    assert is_installed(tmp_path) is False
    install_hook(tmp_path)
    assert is_installed(tmp_path) is True


def test_is_installed_false_when_hook_exists_without_marker(tmp_path):
    _init_repo(tmp_path)
    hook = _hook_path(tmp_path)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho hi\n")
    assert is_installed(tmp_path) is False


def test_uninstall_removes_mship_block_preserves_other_content(tmp_path):
    _init_repo(tmp_path)
    hook = _hook_path(tmp_path)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho 'user step'\n")
    install_hook(tmp_path)
    uninstall_hook(tmp_path)
    content = hook.read_text()
    assert "user step" in content
    assert HOOK_MARKER_BEGIN not in content
    assert HOOK_MARKER_END not in content


def test_uninstall_on_file_without_marker_is_noop(tmp_path):
    _init_repo(tmp_path)
    hook = _hook_path(tmp_path)
    hook.parent.mkdir(parents=True, exist_ok=True)
    original = "#!/bin/sh\necho hi\n"
    hook.write_text(original)
    uninstall_hook(tmp_path)
    assert hook.read_text() == original


def test_install_when_git_dir_missing_raises(tmp_path):
    # No `.git` at all — hook path's parent doesn't exist and we can't install blindly
    with pytest.raises((FileNotFoundError, OSError)):
        install_hook(tmp_path)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_hooks.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `core/hooks.py`**

Create `src/mship/core/hooks.py`:
```python
"""Install / uninstall / detect the mship pre-commit hook.

The hook is a small POSIX shell block wrapped in MSHIP-BEGIN/END markers so it
coexists with user hooks. We never overwrite foreign content; we append or
strip our block as needed.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path


HOOK_MARKER_BEGIN = "# MSHIP-BEGIN"
HOOK_MARKER_END = "# MSHIP-END"

HOOK_BLOCK = f"""{HOOK_MARKER_BEGIN} — managed by mship; edit outside this block is fine
if command -v mship >/dev/null 2>&1; then
    toplevel="$(git rev-parse --show-toplevel)"
    mship _check-commit "$toplevel" || exit 1
fi
{HOOK_MARKER_END}
"""

_NEW_FILE_TEMPLATE = f"""#!/bin/sh
# git pre-commit hook
{HOOK_BLOCK}"""


def _hook_path(git_root: Path) -> Path:
    return git_root / ".git" / "hooks" / "pre-commit"


def is_installed(git_root: Path) -> bool:
    """True if the hook file contains our marker block."""
    path = _hook_path(git_root)
    if not path.exists():
        return False
    return HOOK_MARKER_BEGIN in path.read_text()


def install_hook(git_root: Path) -> None:
    """Install the hook at `<git_root>/.git/hooks/pre-commit`.

    Idempotent when our marker is already present. If a user hook exists
    without our marker, append the MSHIP block after the existing content.
    """
    hooks_dir = git_root / ".git" / "hooks"
    if not hooks_dir.exists():
        # .git/hooks is created by `git init`; if it's missing, this isn't
        # a git repo (or an unusual setup). Don't fabricate it.
        raise FileNotFoundError(f"git hooks dir not found: {hooks_dir}")

    path = _hook_path(git_root)

    if path.exists():
        content = path.read_text()
        if HOOK_MARKER_BEGIN in content:
            _chmod_executable(path)
            return
        # Append MSHIP block after existing content. Guarantee a blank line first.
        if not content.endswith("\n"):
            content += "\n"
        if not content.endswith("\n\n"):
            content += "\n"
        content += HOOK_BLOCK
        path.write_text(content)
    else:
        path.write_text(_NEW_FILE_TEMPLATE)

    _chmod_executable(path)


def uninstall_hook(git_root: Path) -> None:
    """Remove our MSHIP block from the hook file, preserving any user content.

    No-op if the file is missing or doesn't contain our marker.
    """
    path = _hook_path(git_root)
    if not path.exists():
        return
    content = path.read_text()
    if HOOK_MARKER_BEGIN not in content:
        return

    begin_idx = content.index(HOOK_MARKER_BEGIN)
    # Find the END marker and the newline after it
    end_search = content.find(HOOK_MARKER_END, begin_idx)
    if end_search == -1:
        # Marker opened but not closed — bail conservatively, don't mutate
        return
    after_end = content.find("\n", end_search)
    after_end = len(content) if after_end == -1 else after_end + 1

    # Also swallow a blank separator line before the block, if present
    cut_start = begin_idx
    if cut_start >= 2 and content[cut_start - 2:cut_start] == "\n\n":
        cut_start -= 1  # keep one newline, drop the extra

    new_content = content[:cut_start] + content[after_end:]
    # Avoid a dangling extra trailing newline
    while new_content.endswith("\n\n"):
        new_content = new_content[:-1]
    path.write_text(new_content)


def _chmod_executable(path: Path) -> None:
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/core/test_hooks.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/hooks.py tests/core/test_hooks.py
git commit -m "feat(hooks): install/uninstall/detect pre-commit MSHIP block"
```

---

## Task 2: `mship _check-commit` hidden command

**Files:**
- Create: `src/mship/cli/internal.py`
- Modify: `src/mship/cli/__init__.py` (register the sub-app)
- Test: `tests/cli/test_check_commit.py`

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_check_commit.py`:
```python
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState


runner = CliRunner()


def _seed(state_dir: Path, task: Task | None = None):
    sm = StateManager(state_dir)
    if task is None:
        sm.save(WorkspaceState())
    else:
        sm.save(WorkspaceState(current_task=task.slug, tasks={task.slug: task}))


def test_check_commit_no_state_file_exits_zero(tmp_path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    try:
        result = runner.invoke(app, ["_check-commit", str(tmp_path)])
        assert result.exit_code == 0, result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_no_active_task_exits_zero(tmp_path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    _seed(tmp_path / ".mothership")  # empty state
    try:
        result = runner.invoke(app, ["_check-commit", str(tmp_path)])
        assert result.exit_code == 0, result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_matching_worktree_exits_zero(tmp_path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["cli"], branch="feat/t",
        worktrees={"cli": wt},
    )
    _seed(tmp_path / ".mothership", task)
    try:
        result = runner.invoke(app, ["_check-commit", str(wt)])
        assert result.exit_code == 0, result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_wrong_toplevel_exits_one_with_paths(tmp_path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    wt_cli = tmp_path / "wt-cli"
    wt_api = tmp_path / "wt-api"
    wt_cli.mkdir()
    wt_api.mkdir()
    task = Task(
        slug="add-labels", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["cli", "api"], branch="feat/add-labels",
        worktrees={"cli": wt_cli, "api": wt_api},
    )
    _seed(tmp_path / ".mothership", task)
    try:
        wrong = tmp_path / "elsewhere"
        wrong.mkdir()
        result = runner.invoke(app, ["_check-commit", str(wrong)])
        assert result.exit_code == 1
        out = result.output
        assert "add-labels" in out
        assert str(wt_cli) in out
        assert str(wt_api) in out
        assert str(wrong) in out
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_check_commit_fails_open_on_corrupt_state(tmp_path):
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    (tmp_path / ".mothership").mkdir()
    (tmp_path / ".mothership" / "state.yaml").write_text("not: valid: yaml: [[[")
    try:
        result = runner.invoke(app, ["_check-commit", str(tmp_path)])
        assert result.exit_code == 0, result.output  # fail-open
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/test_check_commit.py -v`
Expected: FAIL — command not registered.

- [ ] **Step 3: Implement `cli/internal.py`**

Create `src/mship/cli/internal.py`:
```python
"""Hidden mship commands — used by hooks and other internal consumers."""
from pathlib import Path

import typer


def register(app: typer.Typer, get_container):
    @app.command(name="_check-commit", hidden=True)
    def check_commit(toplevel: str = typer.Argument(..., help="git rev-parse --show-toplevel value")):
        """Exit 0 if committing at `toplevel` is allowed under the active task.

        Fail-open on any exception: corrupt state, missing config, etc. -> exit 0.
        """
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

        try:
            tl = Path(toplevel).resolve()
            allowed = {Path(p).resolve() for p in task.worktrees.values()}
        except (OSError, RuntimeError):
            raise typer.Exit(code=0)

        if tl in allowed:
            raise typer.Exit(code=0)

        import sys
        sys.stderr.write(
            f"\u26d4 mship: refusing commit — this is not a worktree for the active task '{task.slug}'.\n"
            f"   Expected one of:\n"
        )
        for repo_name in sorted(task.worktrees.keys()):
            wt = Path(task.worktrees[repo_name]).resolve()
            sys.stderr.write(f"     {wt} ({repo_name})\n")
        sys.stderr.write(
            f"   Current: {tl}\n"
            f"   cd into the correct worktree, or use `git commit --no-verify` to override.\n"
        )
        raise typer.Exit(code=1)
```

In `src/mship/cli/__init__.py`, register alongside other sub-apps:
```python
from mship.cli import internal as _internal_mod
...
_internal_mod.register(app, get_container)
```

(Add the import grouped with the other `_*_mod` imports; the register call grouped with other register calls. Follow existing pattern.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/cli/test_check_commit.py -v`
Expected: PASS (5 tests).

Then: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/internal.py src/mship/cli/__init__.py \
        tests/cli/test_check_commit.py
git commit -m "feat(cli): add hidden mship _check-commit command for pre-commit hook"
```

---

## Task 3: `mship init` installs hooks + `--install-hooks` flag

**Files:**
- Modify: `src/mship/cli/init.py`
- Test: `tests/test_hook_integration.py` (new)

- [ ] **Step 1: Write the failing integration tests**

`tests/test_hook_integration.py`:
```python
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.hooks import HOOK_MARKER_BEGIN, is_installed


runner = CliRunner()


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True, env=env)


@pytest.fixture
def workspace_for_hooks(tmp_path: Path):
    """Fresh single-repo workspace with a real git init."""
    repo = tmp_path / "cli"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks:\n  setup:\n    cmds:\n      - echo setup\n")
    (repo / "README.md").write_text("cli\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")

    (tmp_path / "mothership.yaml").write_text(
        "workspace: hooktest\n"
        "repos:\n"
        "  cli:\n"
        "    path: ./cli\n"
        "    type: service\n"
    )
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    yield tmp_path, repo
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_install_hooks_flag_installs_on_every_git_root(workspace_for_hooks):
    tmp_path, repo = workspace_for_hooks
    result = runner.invoke(app, ["init", "--install-hooks"])
    assert result.exit_code == 0, result.output
    assert is_installed(repo)


def test_install_hooks_is_idempotent(workspace_for_hooks):
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])
    before = (repo / ".git" / "hooks" / "pre-commit").read_text()
    runner.invoke(app, ["init", "--install-hooks"])
    after = (repo / ".git" / "hooks" / "pre-commit").read_text()
    assert before == after


def test_commit_outside_task_worktree_refused(workspace_for_hooks):
    """End-to-end: spawn creates a worktree; a commit in the main checkout is refused."""
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])
    r = runner.invoke(app, ["spawn", "add avatars", "--repos", "cli", "--force-audit", "--skip-setup"])
    assert r.exit_code == 0, r.output

    # Make a change in the main checkout (wrong place)
    (repo / "new.py").write_text("print('hi')\n")
    _git(repo, "add", "new.py")

    # Attempt commit — hook should refuse
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    result = subprocess.run(
        ["git", "commit", "-m", "should refuse"],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "add-avatars" in result.stderr or "refusing commit" in result.stderr.lower()


def test_commit_inside_worktree_succeeds(workspace_for_hooks):
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])
    r = runner.invoke(app, ["spawn", "inside ok", "--repos", "cli", "--force-audit", "--skip-setup"])
    assert r.exit_code == 0, r.output

    from mship.core.state import StateManager
    state = StateManager(tmp_path / ".mothership").load()
    wt = Path(state.tasks["inside-ok"].worktrees["cli"])
    assert wt.exists()

    # Commit in the worktree — hook should pass
    (wt / "new.py").write_text("print('hi')\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "new.py"], cwd=wt, check=True, capture_output=True, env=env)
    result = subprocess.run(
        ["git", "commit", "-m", "from worktree"],
        cwd=wt, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_no_verify_bypasses_hook(workspace_for_hooks):
    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])
    runner.invoke(app, ["spawn", "bypass test", "--repos", "cli", "--force-audit", "--skip-setup"])

    (repo / "new.py").write_text("x\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _git(repo, "add", "new.py")
    result = subprocess.run(
        ["git", "commit", "--no-verify", "-m", "bypassed"],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_hook_integration.py -v`
Expected: FAIL — `--install-hooks` flag missing.

- [ ] **Step 3: Add `--install-hooks` flag to `init`**

In `src/mship/cli/init.py`, modify the `init` command to:

1. Add the flag to the signature. If the existing `init` command has a large set of flags, add `--install-hooks` alongside them:

```python
    @app.command()
    def init(
        ...existing parameters...,
        install_hooks_only: bool = typer.Option(
            False, "--install-hooks",
            help="Only install git hooks on every known git root (skip the rest of init).",
        ),
    ):
```

2. At the top of the function body (after resolving container / output), branch:

```python
        if install_hooks_only:
            from mship.core.hooks import install_hook
            container = get_container()
            config = container.config()
            # Dedupe effective git roots
            roots: set[Path] = set()
            for name, repo in config.repos.items():
                if repo.git_root is not None and repo.git_root in config.repos:
                    root = Path(config.repos[repo.git_root].path).resolve()
                else:
                    root = Path(repo.path).resolve()
                roots.add(root)
            installed: list[Path] = []
            failed: list[tuple[Path, str]] = []
            for root in sorted(roots):
                try:
                    install_hook(root)
                    installed.append(root)
                except FileNotFoundError as e:
                    failed.append((root, str(e)))
                except Exception as e:
                    failed.append((root, str(e)))
            for r in installed:
                output.success(f"hook installed: {r}/.git/hooks/pre-commit")
            for r, err in failed:
                output.error(f"hook install failed: {r}: {err}")
            raise typer.Exit(code=1 if failed else 0)
```

3. Also run `install_hook` at the END of the normal init flow, after the workspace config is written. If the init flow doesn't have access to the config yet (because it's writing it), reuse the deduping logic to walk `config.repos` after load. Wrap in a try/except so hook install failures don't wreck init — log a warning, proceed.

Use a small helper in the same file to avoid duplicating the dedupe logic:

```python
def _unique_git_roots(config) -> list[Path]:
    from pathlib import Path as _P
    roots: set[_P] = set()
    for name, repo in config.repos.items():
        if repo.git_root is not None and repo.git_root in config.repos:
            roots.add(_P(config.repos[repo.git_root].path).resolve())
        else:
            roots.add(_P(repo.path).resolve())
    return sorted(roots)
```

And in the post-init-success block:
```python
            from mship.core.hooks import install_hook
            for root in _unique_git_roots(config):
                try:
                    install_hook(root)
                except Exception as e:
                    output.print(f"[yellow]warning: could not install hook at {root}: {e}[/yellow]")
```

(Be pragmatic — the exact insertion point depends on the current init code. The structural goal: dedupe git roots, install each, warn on per-root failure, don't abort init.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_hook_integration.py -v`
Expected: PASS (5 tests).

Then: `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/init.py tests/test_hook_integration.py
git commit -m "feat(init): install pre-commit hook on every git root; add --install-hooks"
```

---

## Task 4: `mship doctor` checks hook presence

**Files:**
- Modify: `src/mship/core/doctor.py`
- Test: `tests/core/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_doctor.py`:
```python
def test_doctor_warns_when_hook_missing(tmp_path):
    """Fresh workspace with a git repo but no hook installed → warn."""
    import subprocess
    from mship.core.config import ConfigLoader
    from mship.core.doctor import DoctorChecker
    from mship.util.shell import ShellRunner

    repo = tmp_path / "cli"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  cli:\n    path: ./cli\n    type: service\n"
    )
    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    checker = DoctorChecker(cfg, ShellRunner())
    report = checker.run()
    hook_checks = [c for c in report.checks if "hook" in c.name.lower()]
    assert hook_checks, "expected a hook-related check"
    missing = [c for c in hook_checks if c.status == "warn"]
    assert missing
    assert any("install-hooks" in c.message or "pre-commit" in c.message.lower() for c in missing)


def test_doctor_passes_when_hook_installed(tmp_path):
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

    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  cli:\n    path: ./cli\n    type: service\n"
    )
    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    checker = DoctorChecker(cfg, ShellRunner())
    report = checker.run()
    hook_checks = [c for c in report.checks if "hook" in c.name.lower()]
    assert any(c.status == "pass" for c in hook_checks)
    # Must not have a warn-level hook check
    assert not any(c.status == "warn" for c in hook_checks)


def test_doctor_dedupes_hook_checks_in_monorepo(tmp_path):
    """Three repos sharing one git_root → one hook check, not three."""
    import subprocess
    import yaml
    from mship.core.config import ConfigLoader
    from mship.core.doctor import DoctorChecker
    from mship.util.shell import ShellRunner

    mono = tmp_path / "mono"
    mono.mkdir()
    (mono / "pkg-a").mkdir()
    (mono / "pkg-b").mkdir()
    subprocess.run(["git", "init", "-q", str(mono)], check=True, capture_output=True)
    for p in (mono, mono / "pkg-a", mono / "pkg-b"):
        (p / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    (tmp_path / "mothership.yaml").write_text(yaml.safe_dump({
        "workspace": "m",
        "repos": {
            "mono":  {"path": "./mono", "type": "service"},
            "pkg_a": {"path": "pkg-a", "type": "library", "git_root": "mono"},
            "pkg_b": {"path": "pkg-b", "type": "library", "git_root": "mono"},
        },
    }))
    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    checker = DoctorChecker(cfg, ShellRunner())
    report = checker.run()
    hook_checks = [c for c in report.checks if "hook" in c.name.lower()]
    assert len(hook_checks) == 1
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_doctor.py -v -k "hook"`
Expected: FAIL — doctor doesn't know about hooks yet.

- [ ] **Step 3: Extend `DoctorChecker.run`**

In `src/mship/core/doctor.py`, inside `DoctorChecker.run`, after the per-repo check loop but before the `gh`/`env_runner` checks, add:

```python
        # Pre-commit hook presence per unique git root
        from mship.core.hooks import is_installed
        from pathlib import Path as _P
        seen_roots: set[_P] = set()
        for name, repo in self._config.repos.items():
            if repo.git_root is not None and repo.git_root in self._config.repos:
                root = _P(self._config.repos[repo.git_root].path).resolve()
            else:
                root = _P(repo.path).resolve()
            if root in seen_roots:
                continue
            seen_roots.add(root)
            if not (root / ".git").exists():
                continue  # doctor already warned about this above
            hook_name = f"hooks/{root.name}"
            if is_installed(root):
                report.checks.append(CheckResult(
                    name=hook_name, status="pass",
                    message=f"pre-commit hook installed at {root}/.git/hooks/pre-commit",
                ))
            else:
                report.checks.append(CheckResult(
                    name=hook_name, status="warn",
                    message=(
                        f"pre-commit hook missing at {root}/.git/hooks/pre-commit. "
                        f"Run `mship init --install-hooks` to install."
                    ),
                ))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/core/test_doctor.py -v`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/doctor.py tests/core/test_doctor.py
git commit -m "feat(doctor): warn when pre-commit hook is missing per git root"
```

---

## Task 5: Documentation

**Files:**
- Modify: `skills/working-with-mothership/SKILL.md`
- Modify: `README.md`

- [ ] **Step 1: Update the skill**

In `skills/working-with-mothership/SKILL.md`, under the "Setup" command block (where `mship init` appears), append:

```
mship init --install-hooks            # (re)install the pre-commit hook on every git root
```

Under the existing "MANDATORY after spawn" paragraph about cd'ing, add a follow-up:

```
The pre-commit hook enforces this at the git level: if you try `git commit` anywhere
except the task's assigned worktree while a task is active, the commit is refused.
Use `git commit --no-verify` to bypass for exceptional cases.
```

In "What NOT to Do", append:

```
- **Don't uninstall the pre-commit hook to work around a refusal** — the hook is refusing because you're in the wrong place. `cd` into the task's worktree instead. If the hook genuinely needs to go, remove the MSHIP-BEGIN..MSHIP-END block from `.git/hooks/pre-commit` manually; `mship doctor` will remind you it's missing.
```

- [ ] **Step 2: Update the README**

In `README.md`, under the CLI cheat sheet's Setup block, add the `--install-hooks` flag to the `mship init` line or as a follow-up line:

```
mship init --install-hooks            # (re)install pre-commit guard on every git root
```

Add a short paragraph to the state-safety narrative section (near the existing worktree-isolation prose):

```
**Pre-commit guard.** `mship init` installs a pre-commit hook on every git root. While
a task is active, the hook refuses commits anywhere except the task's worktrees —
making "committed to main instead of the worktree" structurally impossible without
explicit bypass (`git commit --no-verify`). Removed cleanly by editing `.git/hooks/pre-commit`;
`mship doctor` warns when the hook is missing.
```

- [ ] **Step 3: Commit**

```bash
git add skills/working-with-mothership/SKILL.md README.md
git commit -m "docs: document pre-commit hook and --install-hooks"
```

---

## Self-Review

**Spec coverage:**
- `core/hooks.py` install/uninstall/detect + MSHIP block: Task 1.
- `_check-commit` hidden command, fail-open, path comparison: Task 2.
- `mship init` installs hooks at end + `--install-hooks` flag: Task 3.
- End-to-end integration tests (commit outside refused, inside succeeds, `--no-verify` bypass): Task 3.
- `mship doctor` per-git-root hook check with monorepo dedupe: Task 4.
- Skill + README documentation: Task 5.

**Placeholder scan:** none.

**Type consistency:**
- `install_hook(git_root: Path) -> None`, `is_installed(git_root: Path) -> bool`, `uninstall_hook(git_root: Path) -> None` match between definition (Task 1) and callers (Tasks 3, 4).
- `HOOK_MARKER_BEGIN` / `HOOK_MARKER_END` exported constants used by tests and doctor.
- `mship _check-commit <toplevel>` signature matches hook script, test CLI calls, and the Python command.
- `_unique_git_roots` (Task 3) and the doctor loop (Task 4) use the same dedupe logic — duplicated intentionally since doctor shouldn't import CLI-layer helpers. If the duplication bothers later maintenance, factor into `core/hooks.py` as `unique_git_roots(config) -> list[Path]`.

**Known deferrals (explicit in spec):**
- `--uninstall-hooks` flag.
- Post-commit audit of `--no-verify` bypasses.
- Non-POSIX platforms beyond best-effort (git-for-windows ships bash).
