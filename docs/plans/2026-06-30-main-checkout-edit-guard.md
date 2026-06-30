# Main-Checkout Edit Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `guard-against-editing-a-repos-main` (approved). Open question q1 resolved: `mship finish` already audits each repo's MAIN checkout (`audit_repos(names=task.affected_repos)` at `cli/worktree.py:972`), so a dirty main checkout already produces a blocking `dirty_worktree` error at both spawn and finish. The audit work (Task 5) is therefore a message enrichment (make it active-task-aware), not a new blocking mechanism.

**Goal:** Stop an agent from editing a repo's MAIN checkout while that repo has an active task, by adding a Claude Code PreToolUse guard hook backed by a pure decision function, plus an active-task-aware audit message.

**Architecture:** A pure core decision function (`edit_guard.evaluate_edit`) answers allow/block from realpath + workspace state. A hidden CLI adapter (`mship _guard-edit`) reads the PreToolUse event JSON from stdin, calls the function, and denies via exit code 2 (stderr → model) or allows via exit 0 — failing OPEN on any uncertainty. An installer merges the hook into `.claude/settings.json` via `mship init --install-hooks`. The audit probe gains an active-task-aware message for a dirty main checkout.

**Tech Stack:** Python 3.14, typer CLI, pytest, `CliRunner` for CLI tests. Run tests with `uv run pytest` (or `mship test`).

---

## File Structure

- **Create** `src/mship/core/edit_guard.py` — pure `evaluate_edit(target, state, config) -> GuardDecision`. No I/O, no env, no git.
- **Modify** `src/mship/cli/internal.py` — add hidden `_guard-edit` command (stdin JSON → decision → exit 0/2; env bypass; fail-open).
- **Modify** `src/mship/core/claude_settings.py` — add `install_pretooluse_guard_hook`; extract a shared `_install_hook_entry` helper used by both installers.
- **Modify** `src/mship/cli/init.py` — install the guard hook alongside the SessionStart hook at every `--install-hooks` / init site.
- **Modify** `src/mship/core/repo_state.py` — `audit_repos` gains `repos_with_active_task`; a dirty main checkout for such a repo gets an active-task-aware message.
- **Modify** `src/mship/cli/audit.py`, `src/mship/cli/worktree.py` (spawn + finish gates) — pass `repos_with_active_task`.
- **Create** tests: `tests/core/test_edit_guard.py`, `tests/cli/test_guard_edit.py`, plus additions to `tests/core/test_claude_settings.py` (or new file), `tests/cli/test_init_hooks.py` (or existing), and `tests/core/test_repo_state.py` (or existing audit tests).
- **Modify** `README.md`, `docs/cli.md` — document the guard + `MSHIP_ALLOW_MAIN_EDIT`.

---

<!-- mship:task id=1 -->
### Task 1: Pure decision function `edit_guard.evaluate_edit`

**Files:**
- Create: `src/mship/core/edit_guard.py`
- Test: `tests/core/test_edit_guard.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_edit_guard.py
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mship.core.edit_guard import GuardDecision, evaluate_edit
from mship.core.state import Task, WorkspaceState


class _Repo:
    def __init__(self, path: Path):
        self.path = path


class _Config:
    def __init__(self, repos: dict[str, Path]):
        self.repos = {name: _Repo(p) for name, p in repos.items()}


def _state(slug: str, repo: str, worktree: Path) -> WorkspaceState:
    t = Task(
        slug=slug, description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=[repo], worktrees={repo: worktree},
        branch=f"feat/{slug}",
    )
    return WorkspaceState(tasks={slug: t})


def _layout(tmp_path: Path):
    main = tmp_path / "main"; (main / "src").mkdir(parents=True)
    wt = tmp_path / ".worktrees" / "t" / "repo" / "src"
    wt.mkdir(parents=True)
    return main, wt.parent


def test_blocks_edit_in_main_checkout_while_task_active(tmp_path: Path):
    main, wt = _layout(tmp_path)
    cfg = _Config({"repo": main})
    state = _state("t", "repo", wt)
    d = evaluate_edit(main / "src" / "x.py", state, cfg)
    assert d.allowed is False
    assert "MAIN checkout" in d.reason
    assert "repo" in d.reason and "t" in d.reason
    # Suggests the corresponding worktree path.
    assert str(wt / "src" / "x.py") in d.reason


def test_allows_edit_in_worktree(tmp_path: Path):
    main, wt = _layout(tmp_path)
    cfg = _Config({"repo": main})
    state = _state("t", "repo", wt)
    assert evaluate_edit(wt / "src" / "x.py", state, cfg) == GuardDecision(allowed=True)


def test_allows_when_repo_has_no_active_task(tmp_path: Path):
    main, _ = _layout(tmp_path)
    cfg = _Config({"repo": main})
    state = WorkspaceState(tasks={})
    assert evaluate_edit(main / "src" / "x.py", state, cfg).allowed is True


def test_allows_edit_outside_any_repo(tmp_path: Path):
    main, wt = _layout(tmp_path)
    cfg = _Config({"repo": main})
    state = _state("t", "repo", wt)
    assert evaluate_edit(tmp_path / "specs" / "a.md", state, cfg).allowed is True


def test_blocks_via_symlink_to_main_checkout(tmp_path: Path):
    main, wt = _layout(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(main, target_is_directory=True)
    cfg = _Config({"repo": main})
    state = _state("t", "repo", wt)
    # Edit addressed through the symlink resolves to the same main checkout.
    assert evaluate_edit(link / "src" / "x.py", state, cfg).allowed is False


def test_blocks_new_file_that_does_not_exist_yet(tmp_path: Path):
    main, wt = _layout(tmp_path)
    cfg = _Config({"repo": main})
    state = _state("t", "repo", wt)
    assert evaluate_edit(main / "src" / "brand_new.py", state, cfg).allowed is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/core/test_edit_guard.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mship.core.edit_guard'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# src/mship/core/edit_guard.py
"""Decide whether an edit may land at a given path while tasks are active.

Pure — no I/O, no env, no git. The CLI adapter in cli/internal.py handles
stdin/JSON/env/exit-code; this module only answers allow-or-block. Prevents the
failure mode where an agent edits a repo's MAIN checkout (reachable via a
symlink or absolute path) instead of the task worktree, silently landing work on
the base branch. See spec guard-against-editing-a-repos-main.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str = ""


def _real(p: Path) -> Path:
    # realpath resolves symlinks AND a not-yet-existing leaf (Write of a new
    # file): the existing prefix is canonicalized, the rest appended verbatim.
    return Path(os.path.realpath(str(p)))


def _within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def evaluate_edit(target, state, config) -> GuardDecision:
    """Block an edit whose realpath is inside a repo's main checkout while that
    repo has an active task and the path is not inside that task's worktree.
    Allows everything else (caller fails open on errors)."""
    rp = _real(Path(target))
    for name, repo in config.repos.items():
        main = _real(Path(repo.path))
        if not _within(rp, main):
            continue
        for slug, task in state.tasks.items():
            if name not in task.affected_repos:
                continue
            wt = task.worktrees.get(name)
            if wt is None:
                continue
            wt_real = _real(Path(wt))
            if _within(rp, wt_real):
                return GuardDecision(allowed=True)
            try:
                rel = rp.relative_to(main)
            except ValueError:
                rel = None
            suggest = wt_real / rel if rel is not None else wt_real
            return GuardDecision(
                allowed=False,
                reason=(
                    f"Editing the MAIN checkout of '{name}' while task "
                    f"'{slug}' is active. Edit here instead:\n  {suggest}\n"
                    f"(set MSHIP_ALLOW_MAIN_EDIT=1 to override.)"
                ),
            )
    return GuardDecision(allowed=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/core/test_edit_guard.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit + journal**

```bash
git add src/mship/core/edit_guard.py tests/core/test_edit_guard.py
git commit -m "feat(guard): pure evaluate_edit decision for main-checkout edits"
mship journal "edit_guard.evaluate_edit + tests passing" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2: Hidden `mship _guard-edit` CLI adapter

**Files:**
- Modify: `src/mship/cli/internal.py` (add command inside the existing `register(app, get_container)`; mirror `_check-push` at `internal.py:218-270` for stdin reading)
- Test: `tests/cli/test_guard_edit.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/cli/test_guard_edit.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


def _bootstrap(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Workspace with one active task; returns (cfg, state_dir, main_repo)."""
    main = tmp_path / "main"; (main / "src").mkdir(parents=True)
    wt = tmp_path / ".worktrees" / "t" / "repo"; (wt / "src").mkdir(parents=True)
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(f"workspace: t\nrepos:\n  repo:\n    path: {main}\n")
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["repo"], worktrees={"repo": wt}, branch="feat/t",
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"t": task}))
    return cfg, state_dir, main


def _override(cfg, state_dir):
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override(); container.config.reset()
    container.state_manager.reset_override(); container.state_manager.reset()
    container.log_manager.reset()


def _event(path: Path, tool: str = "Edit") -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                       "tool_input": {"file_path": str(path)}})


def test_blocks_edit_in_main_checkout(tmp_path: Path):
    cfg, state_dir, main = _bootstrap(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_guard-edit"], input=_event(main / "src" / "x.py"))
        assert result.exit_code == 2
        assert "MAIN checkout" in result.output
    finally:
        _reset()


def test_allows_edit_in_worktree(tmp_path: Path):
    cfg, state_dir, main = _bootstrap(tmp_path)
    wt_file = tmp_path / ".worktrees" / "t" / "repo" / "src" / "x.py"
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_guard-edit"], input=_event(wt_file))
        assert result.exit_code == 0
    finally:
        _reset()


def test_env_override_allows(tmp_path: Path, monkeypatch):
    cfg, state_dir, main = _bootstrap(tmp_path)
    monkeypatch.setenv("MSHIP_ALLOW_MAIN_EDIT", "1")
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_guard-edit"], input=_event(main / "src" / "x.py"))
        assert result.exit_code == 0
    finally:
        _reset()


def test_malformed_json_fails_open(tmp_path: Path):
    cfg, state_dir, _ = _bootstrap(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_guard-edit"], input="not json{{")
        assert result.exit_code == 0
    finally:
        _reset()


def test_no_file_path_fails_open(tmp_path: Path):
    cfg, state_dir, _ = _bootstrap(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_guard-edit"], input=json.dumps({"tool_input": {}}))
        assert result.exit_code == 0
    finally:
        _reset()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/cli/test_guard_edit.py -q`
Expected: FAIL — `_guard-edit` is not a registered command (exit code 2 from typer "No such command", message differs from `MAIN checkout`), and the allow cases get the same usage error.

- [ ] **Step 3: Add imports at the top of `src/mship/cli/internal.py`**

```python
import os
import sys
import json
```
(Add any not already imported. `sys` is already imported for `_check-push`; add `os` and `json` if absent.)

- [ ] **Step 4: Add the command inside `register(app, get_container)` in `src/mship/cli/internal.py`**

```python
    @app.command(name="_guard-edit", hidden=True)
    def _guard_edit():
        """PreToolUse guard: refuse edits to a repo's MAIN checkout while a task
        is active. Reads the Claude Code hook event JSON from stdin. Denies with
        exit code 2 (stderr shown to the model); allows with exit 0. Fails OPEN
        on any error — never block on uncertainty."""
        from mship.core.edit_guard import evaluate_edit

        if os.environ.get("MSHIP_ALLOW_MAIN_EDIT") == "1":
            raise typer.Exit(code=0)
        try:
            raw = sys.stdin.read()
            event = json.loads(raw) if raw.strip() else {}
            tool_input = event.get("tool_input") or {}
            target = tool_input.get("file_path") or tool_input.get("notebook_path")
            if not target:
                raise typer.Exit(code=0)
            container = get_container(required=False)
            if container is None:
                raise typer.Exit(code=0)
            state = container.state_manager().load()
            config = container.config()
            decision = evaluate_edit(target, state, config)
        except typer.Exit:
            raise
        except Exception:
            raise typer.Exit(code=0)  # fail open
        if decision.allowed:
            raise typer.Exit(code=0)
        sys.stderr.write(decision.reason + "\n")
        raise typer.Exit(code=2)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/cli/test_guard_edit.py -q`
Expected: PASS (5 passed).

- [ ] **Step 6: Verify the deny exit code survives the CLI wrapper**

Run (from anywhere inside this worktree):
```bash
echo '{"tool_input":{"file_path":"/no/such/path"}}' | uv run mship _guard-edit; echo "exit=$?"
```
Expected: `exit=0` (path not in any repo → allow). This confirms the `_should_silent_exit` wrapper in `cli/__init__.py` does not swallow or rewrite the exit code for the now-registered `_guard-edit`.

- [ ] **Step 7: Commit + journal**

```bash
git add src/mship/cli/internal.py tests/cli/test_guard_edit.py
git commit -m "feat(guard): mship _guard-edit PreToolUse adapter (fail-open)"
mship journal "_guard-edit adapter + tests passing" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3: `install_pretooluse_guard_hook` installer

**Files:**
- Modify: `src/mship/core/claude_settings.py` (extract shared helper; add guard installer)
- Test: `tests/core/test_claude_settings_guard.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_claude_settings_guard.py
from __future__ import annotations

import json
from pathlib import Path

from mship.core.claude_settings import (
    install_pretooluse_guard_hook, install_session_hook,
    GUARD_COMMAND, GUARD_MATCHER,
)


def _settings(ws: Path) -> dict:
    return json.loads((ws / ".claude" / "settings.json").read_text())


def test_installs_pretooluse_guard_into_fresh_settings(tmp_path: Path):
    assert install_pretooluse_guard_hook(tmp_path) == "installed"
    pre = _settings(tmp_path)["hooks"]["PreToolUse"]
    entry = pre[0]
    assert entry["matcher"] == GUARD_MATCHER
    assert entry["hooks"][0]["command"] == GUARD_COMMAND


def test_install_guard_is_idempotent(tmp_path: Path):
    assert install_pretooluse_guard_hook(tmp_path) == "installed"
    assert install_pretooluse_guard_hook(tmp_path) == "up to date"
    assert len(_settings(tmp_path)["hooks"]["PreToolUse"]) == 1


def test_guard_preserves_existing_session_hook(tmp_path: Path):
    install_session_hook(tmp_path)
    install_pretooluse_guard_hook(tmp_path)
    data = _settings(tmp_path)
    assert "SessionStart" in data["hooks"]
    assert "PreToolUse" in data["hooks"]


def test_guard_tolerates_malformed_settings(tmp_path: Path):
    cdir = tmp_path / ".claude"; cdir.mkdir()
    (cdir / "settings.json").write_text("{not json")
    assert "skipped" in install_pretooluse_guard_hook(tmp_path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/core/test_claude_settings_guard.py -q`
Expected: FAIL — `ImportError: cannot import name 'install_pretooluse_guard_hook'`.

- [ ] **Step 3: Refactor `src/mship/core/claude_settings.py` to a shared helper + add the guard installer**

Replace the body so both installers share one merge routine (preserves `install_session_hook` behavior exactly):

```python
SESSION_COMMAND = "mship _session-context"
GUARD_COMMAND = "mship _guard-edit"
GUARD_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"


def _install_hook_entry(workspace_root: Path, event_key: str, entry: dict, command: str) -> str:
    """Idempotently merge `entry` into hooks.<event_key> of
    <workspace_root>/.claude/settings.json, deduped by `command`. Preserves all
    existing keys/hooks; tolerates a missing/malformed file."""
    cdir = Path(workspace_root) / ".claude"
    cdir.mkdir(parents=True, exist_ok=True)
    settings_path = cdir / "settings.json"

    data: dict = {}
    if settings_path.is_file():
        raw = settings_path.read_text()
        if raw.strip():
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError:
                return "skipped (settings.json is not valid JSON — fix it, then re-run)"
            if not isinstance(loaded, dict):
                return "skipped (settings.json is not a JSON object)"
            data = loaded

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = data["hooks"] = {}
    lst = hooks.setdefault(event_key, [])
    if not isinstance(lst, list):
        lst = hooks[event_key] = []

    already = any(
        h.get("command") == command
        for e in lst if isinstance(e, dict)
        for h in (e.get("hooks") or []) if isinstance(h, dict)
    )
    if already:
        return "up to date"

    lst.append(entry)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return "installed"


def install_session_hook(workspace_root: Path) -> str:
    return _install_hook_entry(
        workspace_root, "SessionStart",
        {"hooks": [{"type": "command", "command": SESSION_COMMAND}]},
        SESSION_COMMAND,
    )


def install_pretooluse_guard_hook(workspace_root: Path) -> str:
    """Idempotently add a PreToolUse guard hook running `mship _guard-edit`."""
    return _install_hook_entry(
        workspace_root, "PreToolUse",
        {"matcher": GUARD_MATCHER, "hooks": [{"type": "command", "command": GUARD_COMMAND}]},
        GUARD_COMMAND,
    )
```

- [ ] **Step 4: Run the new tests AND the existing session-hook tests**

Run: `uv run pytest tests/core/test_claude_settings_guard.py -q && uv run pytest -q -k claude_settings`
Expected: PASS, including any pre-existing `install_session_hook` tests (behavior unchanged).

- [ ] **Step 5: Commit + journal**

```bash
git add src/mship/core/claude_settings.py tests/core/test_claude_settings_guard.py
git commit -m "feat(guard): install_pretooluse_guard_hook via shared merge helper"
mship journal "guard hook installer + shared helper; session-hook tests still green" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=4 -->
### Task 4: Wire the guard installer into `mship init --install-hooks`

**Files:**
- Modify: `src/mship/cli/init.py` (import + a combined agent-hooks helper; call it at the three existing session-hook sites: the `--install-hooks` short-circuit `init.py:69`, and the two normal-init sites near `init.py:138` and `init.py:308`)
- Test: `tests/cli/test_init_guard_hook.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/cli/test_init_guard_hook.py
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def test_install_hooks_installs_pretooluse_guard(tmp_path: Path):
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["init", "--install-hooks"])
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert "PreToolUse" in data["hooks"]
        assert any(
            h.get("command") == "mship _guard-edit"
            for e in data["hooks"]["PreToolUse"]
            for h in e.get("hooks", [])
        )
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override(); container.config.reset()
        container.state_manager.reset_override(); container.state_manager.reset()
        container.log_manager.reset()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/cli/test_init_guard_hook.py -q`
Expected: FAIL — `KeyError: 'PreToolUse'` (only SessionStart is installed today).

- [ ] **Step 3: Update `src/mship/cli/init.py`**

Change the import line:
```python
from mship.core.claude_settings import install_session_hook, install_pretooluse_guard_hook
```

Replace the `_install_session_hook_with_output` helper with a combined one that installs both agent hooks:
```python
def _install_agent_hooks_with_output(ws_root: Path, output: Output) -> None:
    settings = f"{ws_root}/.claude/settings.json"
    try:
        output.success(f"SessionStart hook @ {settings}: {install_session_hook(ws_root)}")
    except Exception as e:
        output.warning(f"SessionStart hook install skipped: {e}")
    try:
        output.success(f"PreToolUse guard hook @ {settings}: {install_pretooluse_guard_hook(ws_root)}")
    except Exception as e:
        output.warning(f"PreToolUse guard hook install skipped: {e}")
```

Update all three call sites that previously called `_install_session_hook_with_output(...)` to call `_install_agent_hooks_with_output(...)` (the `--install-hooks` short-circuit near `init.py:69`, plus the two normal-init sites near `init.py:138` and `init.py:308`). Use Grep to confirm there are exactly three:
```bash
rg -n "_install_session_hook_with_output" src/mship/cli/init.py
```

- [ ] **Step 4: Run the test + existing init tests**

Run: `uv run pytest tests/cli/test_init_guard_hook.py -q && uv run pytest -q -k init`
Expected: PASS (existing init/SessionStart tests still green; the guard now installs too).

- [ ] **Step 5: Commit + journal**

```bash
git add src/mship/cli/init.py tests/cli/test_init_guard_hook.py
git commit -m "feat(guard): install PreToolUse guard alongside SessionStart in init"
mship journal "init --install-hooks now installs the guard hook" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=5 -->
### Task 5: Active-task-aware audit message for a dirty main checkout

The blocking already exists (`dirty_worktree` is an `error` and the spawn/finish gate blocks on it). This task makes the message point at the worktree when the dirty repo has an active task, and adds a test that locks ac6 (the gate blocks).

**Files:**
- Modify: `src/mship/core/repo_state.py` (`audit_repos` gains `repos_with_active_task: frozenset[str] = frozenset()`; when a repo in that set has a `dirty_worktree` issue, append the active-task hint to its message)
- Modify: `src/mship/cli/audit.py`, `src/mship/cli/worktree.py` (spawn gate ~`:340`, finish gate ~`:972`) to pass `repos_with_active_task`
- Test: add to `tests/core/test_repo_state.py` (or a new `tests/core/test_audit_active_task.py`)

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_audit_active_task.py
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from mship.core.repo_state import audit_repos


class _Repo:
    def __init__(self, path: Path):
        self.path = path
        self.git_root = None
        self.expected_branch = None
        self.base_branch = "main"
        self.allow_dirty = False
        self.allow_extra_worktrees = False


class _Config:
    def __init__(self, repos):
        self.workspace = "t"
        self.repos = repos


def _git(args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _dirty_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"; repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    (repo / "f.txt").write_text("a\n")
    _git(["add", "."], repo); _git(["commit", "-qm", "init"], repo)
    (repo / "f.txt").write_text("b\n")  # tracked modification
    return repo


def _find(report, name):
    return next(r for r in report.repos if r.name == name)


def test_dirty_main_with_active_task_has_hint(tmp_path):
    repo = _dirty_repo(tmp_path)
    from mship.core.shell import Shell  # adjust import to the real Shell entry
    cfg = _Config({"repo": _Repo(repo)})
    report = audit_repos(cfg, Shell(), names=["repo"],
                         repos_with_active_task=frozenset({"repo"}))
    issues = {i.code: i.message for i in _find(report, "repo").issues}
    assert "dirty_worktree" in issues
    assert "task" in issues["dirty_worktree"].lower()
    assert "worktree" in issues["dirty_worktree"].lower()


def test_dirty_main_without_active_task_has_no_hint(tmp_path):
    repo = _dirty_repo(tmp_path)
    from mship.core.shell import Shell
    cfg = _Config({"repo": _Repo(repo)})
    report = audit_repos(cfg, Shell(), names=["repo"])  # no active task
    msg = {i.code: i.message for i in _find(report, "repo").issues}["dirty_worktree"]
    assert "edit in its worktree" not in msg.lower()
```

> Note: confirm the real `Shell` constructor used by `container.shell()` (Grep `def shell` in `container.py`); adjust the import/instantiation in the test to match. The existing audit tests in the repo already build a shell — copy their pattern.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/core/test_audit_active_task.py -q`
Expected: FAIL — `audit_repos() got an unexpected keyword argument 'repos_with_active_task'`.

- [ ] **Step 3: Implement in `src/mship/core/repo_state.py`**

Add the parameter to `audit_repos`:
```python
def audit_repos(
    config,
    shell,
    names: Iterable[str] | None = None,
    known_worktree_paths: frozenset[Path] = frozenset(),
    local_only: bool = False,
    repos_with_active_task: frozenset[str] = frozenset(),
) -> AuditReport:
```

Where each repo's `RepoAudit` is assembled (the per-repo dispatch near `repo_state.py:343-358`), post-process its issues: if the repo name is in `repos_with_active_task`, replace any `dirty_worktree` issue with an enriched message. Add a small helper above `audit_repos`:
```python
def _enrich_active_task(issues: tuple[Issue, ...], has_active_task: bool) -> tuple[Issue, ...]:
    if not has_active_task:
        return issues
    out = []
    for i in issues:
        if i.code == "dirty_worktree":
            out.append(Issue(i.code, i.severity,
                             i.message + " — a task is active for this repo; "
                             "edit in its worktree, not the main checkout "
                             "(see `mship worktrees`)"))
        else:
            out.append(i)
    return tuple(out)
```
and apply it when constructing each `RepoAudit`:
```python
issues = _enrich_active_task(issues, name in repos_with_active_task)
```

- [ ] **Step 4: Thread the parameter from the callers**

In `src/mship/cli/audit.py`, after loading `known`, derive and pass the set:
```python
try:
    state = container.state_manager().load()
    active = frozenset(
        r for task in state.tasks.values() for r in task.affected_repos
    )
except Exception:
    active = frozenset()
report = audit_repos(config, shell, names=names,
                     known_worktree_paths=known, repos_with_active_task=active)
```
Do the same at the spawn gate (`worktree.py:340`) and finish gate (`worktree.py:972`) — both already have access to the task/state; pass `repos_with_active_task` built from the active tasks (for the finish gate, `frozenset(task.affected_repos)` for the current task is sufficient; for spawn, build from all active tasks).

- [ ] **Step 5: Run the new tests + the full audit test module**

Run: `uv run pytest tests/core/test_audit_active_task.py -q && uv run pytest -q -k repo_state`
Expected: PASS; pre-existing `dirty_worktree`/`dirty_untracked` assertions still pass (base message preserved; only a suffix is appended for active-task repos).

- [ ] **Step 6: Commit + journal**

```bash
git add src/mship/core/repo_state.py src/mship/cli/audit.py src/mship/cli/worktree.py tests/core/test_audit_active_task.py
git commit -m "feat(guard): active-task-aware dirty-main-checkout audit message"
mship journal "audit dirty_worktree now points active-task repos at the worktree" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=6 -->
### Task 6: Document the guard

**Files:**
- Modify: `README.md` (the pre-commit-hook / coordination paragraph), `docs/cli.md` (near the hooks/init entries)

- [ ] **Step 1: Add a README note**

In the section describing the pre-commit hook (search `pre-commit` in `README.md`), add a sentence:
> mship also installs a Claude Code **PreToolUse guard** (`mship _guard-edit`, added by `mship init --install-hooks`) that refuses an Edit/Write to a repo's main checkout while that repo has an active task — closing the gap that the git pre-commit hook can't see, since edit tools bypass git. Override a one-off with `MSHIP_ALLOW_MAIN_EDIT=1`.

- [ ] **Step 2: Add a `docs/cli.md` line**

Near the `mship init --install-hooks` entry, add:
```
# `mship init --install-hooks` also installs a Claude Code PreToolUse guard
# (mship _guard-edit) that blocks edits to a repo's main checkout while it has
# an active task. Bypass: MSHIP_ALLOW_MAIN_EDIT=1.
```

- [ ] **Step 3: Verify docs render / no broken references**

Run: `rg -n "MSHIP_ALLOW_MAIN_EDIT|_guard-edit" README.md docs/cli.md`
Expected: both files reference the guard.

- [ ] **Step 4: Commit + journal**

```bash
git add README.md docs/cli.md
git commit -m "docs(guard): document the PreToolUse main-checkout guard"
mship journal "documented the edit guard in README + cli.md" --action committed
```
<!-- /mship:task -->

---

## Final verification (after all tasks)

- [ ] Run the full suite: `mship test`. Expected: all pass (baseline was 1749).
- [ ] Manual smoke from inside this worktree:
  ```bash
  echo "{\"tool_input\":{\"file_path\":\"$(realpath ../../../../mothership)/src/mship/core/dispatch.py\"}}" | uv run mship _guard-edit; echo "exit=$?"
  ```
  Expected: `exit=2` with a message naming this task's worktree (an edit to the mothership MAIN checkout is blocked while this task is active).
- [ ] `mship audit --repos mothership` while the main checkout is clean → no `dirty_worktree`. (Don't dirty the main checkout to test; rely on the unit test for the message.)
