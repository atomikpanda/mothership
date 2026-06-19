# Enforcement gate for untasked work — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `enforcement-gate` (MOS-189 / GH #216) — approved. Full rationale at `specs/2026-06-19-enforcement-gate.md` in the workspace metarepo.

**Goal:** Make untasked feature work deterministically hard to commit/push and remind the agent to spawn at session start — by fixing the hook PATH bug (so the existing commit gate fires), adding a pre-push gate, and injecting a session-start notice, all with a logged escape hatch.

**Architecture:** A new `core/gate.py` holds the cross-cutting bypass + notice logic. `hooks.py` is refactored so hook bodies resolve mship reliably (install-time absolute path + PATH fallback) and the *enforcing* hooks fail closed; a new `pre-push` hook is added. `internal.py` gains `_check-push` and `_session-context` and makes `_check-commit` bypass-aware. `core/claude_settings.py` installs a SessionStart hook into the workspace `.claude/settings.json`, wired from `init`.

**Tech Stack:** Python 3.14, Typer, pytest, `uv`. POSIX sh (hook bodies). Git hooks (pre-commit/pre-push/post-*) + a Claude Code SessionStart hook.

**Where to work:** paths are relative to the `mothership` worktree:
`/home/bailey/development/repos/mship-workspace/.worktrees/enforcement-gate/mothership`. `cd` there; task `enforcement-gate`, branch `feat/enforcement-gate`. Targeted tests: `uv run pytest …`; full suite: `mship test`.

---

## Verified codebase facts

- `_check-commit` (`src/mship/cli/internal.py`) **already** refuses commits when `not state.tasks` and `_staged_source_paths(toplevel)` (staged files under `src/`/`tests/` at the workspace root) is non-empty. It loads state via `get_container(required=False)` → `container.state_manager().load()`, and **fails open** (exit 0) on any exception. This is the gate the PATH bug neuters.
- `hooks.py`: `_HOOKS: dict[str, tuple[str, str]]` maps name → (header, body). Bodies currently guard with `command -v mship`. `install_hook(git_root)` iterates `_HOOKS` calling `_install_one`. Blocks are wrapped in `# MSHIP-BEGIN`/`# MSHIP-END` markers; `_install_one` appends/refreshes idempotently. `HOOK_BLOCK = _block(_PRE_COMMIT_BODY)` exists at module bottom (check/refresh its consumers when bodies become builders).
- `init.py` `--install-hooks` path iterates outcomes for the tuple `("pre-commit", "post-commit", "post-checkout")` to print results, then `raise typer.Exit`. `workspace_root = container.config_path().parent`.
- `config.branch_pattern` default `"feat/{slug}"`. Task branch is `state.tasks[slug].branch`.

## File structure

| File | Responsibility | Task |
|---|---|---|
| `src/mship/core/gate.py` (new) | `resolve_bypass`, `record_bypass`, `no_task_notice` (+ `NO_TASK_NOTICE`) | 1 |
| `src/mship/core/hooks.py` | reliable mship resolution + fail-closed enforcing hooks; add `pre-push` | 2 |
| `src/mship/cli/internal.py` | `_check-push`, `_session-context`, bypass-aware `_check-commit` | 3 |
| `src/mship/core/claude_settings.py` (new) | idempotent SessionStart install into `.claude/settings.json` | 4 |
| `src/mship/cli/init.py` | install the Claude session hook + show pre-push outcome | 5 |
| `tests/core/test_gate.py` (new) | gate unit tests | 1 |
| `tests/core/test_hooks.py` | hook-body resolution/fail-closed/pre-push | 2 |
| `tests/cli/test_internal.py` | `_check-push` / `_session-context` / bypass | 3 |
| `tests/core/test_claude_settings.py` (new) | settings install idempotency | 4 |

---

## Task 1: `core/gate.py` — bypass + session notice

**Files:** Create `src/mship/core/gate.py`; Test `tests/core/test_gate.py`.

- [ ] **Step 1: failing tests** — create `tests/core/test_gate.py`:

```python
import json
from pathlib import Path

from mship.core.gate import resolve_bypass, record_bypass, no_task_notice, NO_TASK_NOTICE


def test_resolve_bypass_unset(monkeypatch):
    monkeypatch.delenv("MSHIP_BYPASS_GATE", raising=False)
    assert resolve_bypass() == (False, "")


def test_resolve_bypass_bare(monkeypatch):
    monkeypatch.setenv("MSHIP_BYPASS_GATE", "1")
    assert resolve_bypass() == (True, "")


def test_resolve_bypass_with_reason(monkeypatch):
    monkeypatch.setenv("MSHIP_BYPASS_GATE", "quick hotfix")
    assert resolve_bypass() == (True, "quick hotfix")


def test_record_bypass_appends_jsonl(tmp_path):
    ws = tmp_path
    (ws / ".mothership").mkdir()
    record_bypass(ws, op="commit", branch="feat/x", reason="r")
    log = ws / ".mothership" / "bypass-log.jsonl"
    line = json.loads(log.read_text().splitlines()[-1])
    assert line["op"] == "commit" and line["branch"] == "feat/x" and line["reason"] == "r"
    assert "ts" in line and "cwd" in line


def _ws_with(tmp_path, tasks: bool):
    ws = tmp_path / "ws"; ws.mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
    )
    (ws / "lib").mkdir(); (ws / "lib" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    sd = ws / ".mothership"; sd.mkdir()
    if tasks:
        # minimal state with one task so state.tasks is non-empty
        (sd / "state.yaml").write_text(
            "tasks:\n  t:\n    slug: t\n    description: d\n    phase: dev\n"
            "    created_at: 2026-01-01T00:00:00+00:00\n    affected_repos: [lib]\n"
            "    branch: feat/t\n"
        )
    return ws


def test_no_task_notice_workspace_no_task(tmp_path):
    ws = _ws_with(tmp_path, tasks=False)
    assert no_task_notice(ws) == NO_TASK_NOTICE


def test_no_task_notice_with_task_is_none(tmp_path):
    ws = _ws_with(tmp_path, tasks=True)
    assert no_task_notice(ws) is None


def test_no_task_notice_outside_workspace_is_none(tmp_path):
    assert no_task_notice(tmp_path) is None
```

- [ ] **Step 2: run, expect ModuleNotFoundError** — `uv run pytest tests/core/test_gate.py -q`.

- [ ] **Step 3: implement** — create `src/mship/core/gate.py`:

```python
"""Deterministic enforcement-gate helpers: bypass resolution + logging, and the
session-start no-active-task notice. See spec enforcement-gate (MOS-189)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

NO_TASK_NOTICE = (
    "mothership workspace with no active task. Run `mship spawn \"<description>\"` "
    "before editing source files — commits/pushes of untasked feature work are gated."
)

_BYPASS_ENV = "MSHIP_BYPASS_GATE"


def resolve_bypass() -> tuple[bool, str]:
    """(bypassed, reason) from MSHIP_BYPASS_GATE. A bare/'1' value → reason ''."""
    val = os.environ.get(_BYPASS_ENV)
    if not val or not val.strip():
        return (False, "")
    reason = val.strip()
    return (True, "" if reason == "1" else reason)


def record_bypass(workspace_root: Path, *, op: str, branch: str, reason: str) -> None:
    """Append a bypass record to <workspace_root>/.mothership/bypass-log.jsonl."""
    sd = Path(workspace_root) / ".mothership"
    sd.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "op": op,
        "branch": branch,
        "reason": reason,
        "cwd": str(Path.cwd()),
    }
    with (sd / "bypass-log.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


def no_task_notice(cwd: Path) -> str | None:
    """Return the notice when `cwd` is in a mship workspace with no active task,
    else None. Fail-open (None) on any error — this is advisory context."""
    try:
        from mship.core.config import ConfigLoader
        from mship.core.state import StateManager
        try:
            config_path = ConfigLoader.discover(Path(cwd))
        except FileNotFoundError:
            return None
        state = StateManager(config_path.parent / ".mothership").load()
        return None if state.tasks else NO_TASK_NOTICE
    except Exception:
        return None
```

- [ ] **Step 4: run, expect pass** — `uv run pytest tests/core/test_gate.py -v`. (If the `state.yaml` fixture shape doesn't load, adjust it to match `StateManager`/`Task` — inspect `src/mship/core/state.py` and use the exact field names; the test's intent is "state has ≥1 task".)

- [ ] **Step 5: commit**

```bash
git add src/mship/core/gate.py tests/core/test_gate.py
git commit -m "feat(gate): bypass resolution+logging and no-task session notice (MOS-189)"
mship journal "gate.py: resolve_bypass/record_bypass/no_task_notice; tests passing" --action committed
```

---

## Task 2: `hooks.py` — reliable resolution, fail-closed, pre-push

**Files:** Modify `src/mship/core/hooks.py`; Test `tests/core/test_hooks.py` (create if absent).

The fix: hook bodies become **builders** parameterized by the install-time mship path, resolve mship reliably at runtime, and the **enforcing** hooks fail closed.

- [ ] **Step 1: failing tests** — add to `tests/core/test_hooks.py`:

```python
from mship.core.hooks import _HOOKS, install_hook, is_installed


def test_pre_push_in_inventory():
    assert "pre-push" in _HOOKS


def test_enforcing_hook_body_resolves_and_fails_closed():
    header, builder = _HOOKS["pre-commit"]
    body = builder("/abs/mship")
    assert "/abs/mship" in body                  # install-time path baked
    assert 'command -v mship' in body            # PATH fallback present
    assert "exit 1" in body                       # fail-closed when unresolved
    assert "MSHIP_BYPASS_GATE" in body            # names the escape hatch


def test_pre_push_body_calls_check_push():
    _header, builder = _HOOKS["pre-push"]
    body = builder("/abs/mship")
    assert "_check-push" in body
    assert "exit 1" in body


def test_advisory_hook_stays_no_op():
    _header, builder = _HOOKS["post-commit"]
    body = builder("/abs/mship")
    assert "|| true" in body                      # never blocks
    assert "exit 1" not in body


def test_install_then_detected(tmp_path):
    import subprocess
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    install_hook(tmp_path)
    assert is_installed(tmp_path)
    pre_push = (tmp_path / ".git" / "hooks" / "pre-push").read_text()
    assert "_check-push" in pre_push
```

- [ ] **Step 2: run, expect failure** — `uv run pytest tests/core/test_hooks.py -v` (KeyError `pre-push` / `_HOOKS` values aren't callable).

- [ ] **Step 3: implement** — in `src/mship/core/hooks.py`:

Replace the four static body constants with builder functions that take the resolved mship path. The **enforcing** resolution prelude (shared by pre-commit and pre-push):

```python
def _resolve_prelude(mship_bin: str) -> str:
    # Reliable resolution: install-time absolute path, then PATH fallback.
    # Enforcing hooks fail closed if mship can't be found (the gate must not
    # silently no-op — that is the MOS-189 bug).
    return (
        f'MSHIP_BIN="{mship_bin}"\n'
        'if [ ! -x "$MSHIP_BIN" ]; then MSHIP_BIN="$(command -v mship 2>/dev/null || true)"; fi\n'
        'if [ -z "$MSHIP_BIN" ]; then\n'
        '    echo "mship: cannot enforce task gate (mship not found). Reinstall hooks (mship init --install-hooks) or set MSHIP_BYPASS_GATE=1 to override." >&2\n'
        '    exit 1\n'
        'fi\n'
    )


def _pre_commit_body(mship_bin: str) -> str:
    return (
        _resolve_prelude(mship_bin)
        + 'toplevel="$(git rev-parse --show-toplevel)"\n'
        + '"$MSHIP_BIN" _check-commit "$toplevel" || exit 1\n'
    )


def _pre_push_body(mship_bin: str) -> str:
    # pre-push receives ref lines on stdin: <local_ref> <local_sha> <remote_ref> <remote_sha>
    return (
        _resolve_prelude(mship_bin)
        + '"$MSHIP_BIN" _check-push || exit 1\n'
    )


def _post_checkout_body(mship_bin: str) -> str:
    return (
        f'MSHIP_BIN="{mship_bin}"\n'
        'if [ ! -x "$MSHIP_BIN" ]; then MSHIP_BIN="$(command -v mship 2>/dev/null || true)"; fi\n'
        'if [ -n "$MSHIP_BIN" ]; then\n'
        '    prev_head="$1"; new_head="$2"; is_branch_checkout="$3"\n'
        '    if [ "$is_branch_checkout" = "1" ]; then\n'
        '        "$MSHIP_BIN" _post-checkout "$prev_head" "$new_head" || true\n'
        '    fi\n'
        'fi\n'
    )


def _post_commit_body(mship_bin: str) -> str:
    return (
        f'MSHIP_BIN="{mship_bin}"\n'
        'if [ ! -x "$MSHIP_BIN" ]; then MSHIP_BIN="$(command -v mship 2>/dev/null || true)"; fi\n'
        '[ -n "$MSHIP_BIN" ] && "$MSHIP_BIN" _journal-commit || true\n'
    )
```

Change the inventory to builders:

```python
_HOOKS: dict[str, tuple[str, "Callable[[str], str]"]] = {
    "pre-commit": ("# git pre-commit hook", _pre_commit_body),
    "pre-push": ("# git pre-push hook", _pre_push_body),
    "post-checkout": ("# git post-checkout hook", _post_checkout_body),
    "post-commit": ("# git post-commit hook", _post_commit_body),
}
```

(Add `from typing import Callable` at the top.) In `install_hook`, resolve the mship path once and build each body:

```python
def install_hook(git_root: Path) -> dict[str, InstallOutcome]:
    import shutil
    mship_bin = shutil.which("mship") or ""
    outcomes: dict[str, InstallOutcome] = {}
    for name, (header, builder) in _HOOKS.items():
        outcomes[name] = _install_one(git_root, name, header, builder(mship_bin))
    return outcomes
```

Update `HOOK_BLOCK` at the module bottom (and any consumer) — derive it from the builder: `HOOK_BLOCK = _block(_pre_commit_body(""))`. Grep for `HOOK_BLOCK` and `_PRE_COMMIT_BODY` usages and update them; `is_installed`/`_one_is_installed` check markers, not body equality, so they're unaffected.

- [ ] **Step 4: run, expect pass** — `uv run pytest tests/core/test_hooks.py -v`.

- [ ] **Step 5: commit**

```bash
git add src/mship/core/hooks.py tests/core/test_hooks.py
git commit -m "feat(hooks): reliable mship resolution + fail-closed enforcing hooks + pre-push (MOS-189)"
mship journal "hooks: builder bodies resolve mship (abs path+PATH) and fail closed for pre-commit/pre-push; added pre-push; tests passing" --action committed
```

---

## Task 3: `internal.py` — `_check-push`, `_session-context`, bypass-aware `_check-commit`

**Files:** Modify `src/mship/cli/internal.py`; Test `tests/cli/test_internal.py` (create if absent).

- [ ] **Step 1: failing tests** — add to `tests/cli/test_internal.py`:

```python
from typer.testing import CliRunner
from mship.cli import app, container

runner = CliRunner()


def _setup(tmp_path, monkeypatch, tasks_yaml=""):
    ws = tmp_path
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
    )
    (ws / "lib").mkdir(); (ws / "lib" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    sd = ws / ".mothership"; sd.mkdir()
    (sd / "state.yaml").write_text(tasks_yaml or "tasks: {}\n")
    container.config_path.override(ws / "mothership.yaml")
    container.state_dir.override(sd)
    container.config.reset(); container.state_manager.reset()
    monkeypatch.chdir(ws)


def _reset():
    container.config_path.reset_override(); container.state_dir.reset_override()
    container.config.reset(); container.state_manager.reset()


def _push_stdin(branch, sha="a" * 40):
    return f"refs/heads/{branch} {sha} refs/heads/{branch} {'0'*40}\n"


def test_check_push_rejects_unregistered_feat_branch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # no tasks
    try:
        r = runner.invoke(app, ["_check-push"], input=_push_stdin("feat/hand-rolled"))
        assert r.exit_code == 1
    finally:
        _reset()


def test_check_push_allows_non_pattern_branch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        r = runner.invoke(app, ["_check-push"], input=_push_stdin("main"))
        assert r.exit_code == 0
    finally:
        _reset()


def test_check_push_allows_delete(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        # delete: local sha all zeros
        r = runner.invoke(app, ["_check-push"], input=_push_stdin("feat/x", sha="0"*40))
        assert r.exit_code == 0
    finally:
        _reset()


def test_check_push_bypass_allows_and_logs(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MSHIP_BYPASS_GATE", "intentional")
    try:
        r = runner.invoke(app, ["_check-push"], input=_push_stdin("feat/x"))
        assert r.exit_code == 0
        log = tmp_path / ".mothership" / "bypass-log.jsonl"
        assert log.exists() and "intentional" in log.read_text()
    finally:
        _reset()


def test_session_context_prints_notice_when_no_task(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)  # no tasks
    try:
        r = runner.invoke(app, ["_session-context"])
        assert r.exit_code == 0
        assert "no active task" in r.stdout.lower()
    finally:
        _reset()
```

(If a registered task branch is needed for an "allow" case, write a `state.yaml` with a task whose `branch: feat/known` and assert `_check-push` allows `feat/known` — match the exact `Task` schema in `src/mship/core/state.py`.)

- [ ] **Step 2: run, expect failure** — `uv run pytest tests/cli/test_internal.py -v` (no such commands).

- [ ] **Step 3: implement** — in `src/mship/cli/internal.py`, inside `register(...)`:

Add bypass-awareness to `_check-commit` — at the very top of `check_commit`, before the existing logic:

```python
        from mship.core.gate import resolve_bypass, record_bypass
        bypassed, reason = resolve_bypass()
        if bypassed:
            try:
                container = get_container(required=False)
                if container is not None:
                    ws_root = Path(container.config_path()).parent
                    record_bypass(ws_root, op="commit", branch="", reason=reason)
            except Exception:
                pass
            raise typer.Exit(code=0)
```

Add `_check-push`:

```python
    @app.command(name="_check-push", hidden=True)
    def check_push():
        """Reject pushing a branch-pattern branch that is not a registered task
        branch. Reads git pre-push ref lines from stdin. Fail-open on error."""
        import sys
        from mship.core.gate import resolve_bypass, record_bypass

        try:
            container = get_container(required=False)
            if container is None:
                raise typer.Exit(code=0)
            config = container.config()
            state = container.state_manager().load()
        except typer.Exit:
            raise
        except Exception:
            raise typer.Exit(code=0)

        prefix = config.branch_pattern.split("{slug}", 1)[0]  # e.g. "feat/"
        task_branches = {t.branch for t in state.tasks.values()}

        offending: list[str] = []
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            local_ref, local_sha = parts[0], parts[1]
            if set(local_sha) == {"0"}:        # delete — nothing pushed
                continue
            if not local_ref.startswith("refs/heads/"):
                continue
            branch = local_ref[len("refs/heads/"):]
            if prefix and branch.startswith(prefix) and branch not in task_branches:
                offending.append(branch)

        if not offending:
            raise typer.Exit(code=0)

        bypassed, reason = resolve_bypass()
        if bypassed:
            ws_root = Path(container.config_path()).parent
            for b in offending:
                record_bypass(ws_root, op="push", branch=b, reason=reason)
            raise typer.Exit(code=0)

        sys.stderr.write(
            "⛔ mship: refusing push — branch(es) not registered to a task: "
            + ", ".join(offending) + "\n"
            "   Spawn a task (mship spawn) so the branch is tracked, or set "
            "MSHIP_BYPASS_GATE=1 (or `git push --no-verify`) to override.\n"
        )
        raise typer.Exit(code=1)
```

Add `_session-context`:

```python
    @app.command(name="_session-context", hidden=True)
    def session_context():
        """Print the no-active-task notice for the SessionStart hook (else nothing)."""
        from mship.core.gate import no_task_notice
        notice = no_task_notice(Path.cwd())
        if notice:
            import sys
            sys.stdout.write(notice + "\n")
        raise typer.Exit(code=0)
```

- [ ] **Step 4: run, expect pass** — `uv run pytest tests/cli/test_internal.py -v`.

- [ ] **Step 5: commit**

```bash
git add src/mship/cli/internal.py tests/cli/test_internal.py
git commit -m "feat(internal): _check-push, _session-context, bypass-aware _check-commit (MOS-189)"
mship journal "internal: _check-push (pattern-branch gate, stdin refs, bypass-logged), _session-context, bypass in _check-commit; tests passing" --action committed
```

---

## Task 4: `core/claude_settings.py` — SessionStart install

**Files:** Create `src/mship/core/claude_settings.py`; Test `tests/core/test_claude_settings.py`.

- [ ] **Step 1: failing tests** — create `tests/core/test_claude_settings.py`:

```python
import json
from pathlib import Path

from mship.core.claude_settings import install_session_hook, SESSION_COMMAND


def _hooks(ws: Path):
    return json.loads((ws / ".claude" / "settings.json").read_text())["hooks"]["SessionStart"]


def test_install_creates_settings(tmp_path):
    install_session_hook(tmp_path)
    entries = _hooks(tmp_path)
    cmds = [h["command"] for e in entries for h in e["hooks"]]
    assert SESSION_COMMAND in cmds


def test_install_is_idempotent(tmp_path):
    install_session_hook(tmp_path)
    install_session_hook(tmp_path)
    entries = _hooks(tmp_path)
    cmds = [h["command"] for e in entries for h in e["hooks"]]
    assert cmds.count(SESSION_COMMAND) == 1


def test_install_preserves_existing_hooks(tmp_path):
    cdir = tmp_path / ".claude"; cdir.mkdir()
    (cdir / "settings.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        "model": "sonnet",
    }))
    install_session_hook(tmp_path)
    data = json.loads((cdir / "settings.json").read_text())
    assert data["model"] == "sonnet"  # untouched
    cmds = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert "echo hi" in cmds and SESSION_COMMAND in cmds
```

- [ ] **Step 2: run, expect ModuleNotFoundError**.

- [ ] **Step 3: implement** — create `src/mship/core/claude_settings.py`:

```python
"""Install a Claude Code SessionStart hook into a workspace's .claude/settings.json
so each session surfaces the no-active-task notice. See spec enforcement-gate."""
from __future__ import annotations

import json
from pathlib import Path

SESSION_COMMAND = "mship _session-context"


def install_session_hook(workspace_root: Path) -> str:
    """Idempotently add a SessionStart hook running `mship _session-context` to
    <workspace_root>/.claude/settings.json. Returns 'installed' or 'up to date'.
    Preserves all existing keys and hooks."""
    cdir = Path(workspace_root) / ".claude"
    cdir.mkdir(parents=True, exist_ok=True)
    settings_path = cdir / "settings.json"

    data: dict = {}
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text() or "{}")
        except json.JSONDecodeError:
            data = {}
    if not isinstance(data, dict):
        data = {}

    hooks = data.setdefault("hooks", {})
    session = hooks.setdefault("SessionStart", [])

    already = any(
        h.get("command") == SESSION_COMMAND
        for entry in session if isinstance(entry, dict)
        for h in entry.get("hooks", []) if isinstance(h, dict)
    )
    if already:
        return "up to date"

    session.append({"hooks": [{"type": "command", "command": SESSION_COMMAND}]})
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return "installed"
```

- [ ] **Step 4: run, expect pass** — `uv run pytest tests/core/test_claude_settings.py -v`.

- [ ] **Step 5: commit**

```bash
git add src/mship/core/claude_settings.py tests/core/test_claude_settings.py
git commit -m "feat(claude): idempotent SessionStart hook install into .claude/settings.json (MOS-189)"
mship journal "claude_settings: install_session_hook idempotent + preserves existing; tests passing" --action committed
```

---

## Task 5: wire the session hook into `init --install-hooks`

**Files:** Modify `src/mship/cli/init.py`.

- [ ] **Step 1: implement** — in the `--install-hooks` short-circuit of `init.py`, after the git-hook install loop and before `raise typer.Exit(...)`, install the Claude session hook once at the workspace root and report it. Add the import (`from mship.core.claude_settings import install_session_hook`) and:

```python
            try:
                ws_root = Path(container.config_path()).parent
                outcome = install_session_hook(ws_root)
                output.success(f"SessionStart hook @ {ws_root}/.claude/settings.json: {outcome}")
            except Exception as e:
                output.warning(f"SessionStart hook install skipped: {e}")
```

Also add `"pre-push"` to the per-root outcome display tuple so its result prints, i.e. change `("pre-commit", "post-commit", "post-checkout")` to `("pre-commit", "pre-push", "post-commit", "post-checkout")`. Do the same for the full (non-`--install-hooks`) init path if it reports hook outcomes; if that path installs hooks silently, just add the `install_session_hook(ws_root)` call once after its install loop.

- [ ] **Step 2: verify** — `uv run mship init --install-hooks` in a workspace prints a `pre-push` line and a `SessionStart hook … installed` line; re-running prints `up to date`. Confirm `.claude/settings.json` has the entry and `.git/hooks/pre-push` exists.

- [ ] **Step 3: full suite** — `mship test` → green (modulo the pre-existing MOS-188 serve test if run from a bare checkout).

- [ ] **Step 4: commit**

```bash
git add src/mship/cli/init.py
git commit -m "feat(init): install SessionStart hook + show pre-push outcome (MOS-189)"
mship journal "init --install-hooks now installs the Claude SessionStart hook + reports pre-push; full suite green" --action committed
```

---

## Final verification
- [ ] `mship test` — full suite green.
- [ ] Manual smoke: in a workspace with no task, `printf 'refs/heads/feat/x %s refs/heads/feat/x %s\n' $(git rev-parse HEAD) $(printf '0%.0s' {1..40}) | uv run mship _check-push` exits 1; `MSHIP_BYPASS_GATE=1 … _check-push` exits 0 and writes `.mothership/bypass-log.jsonl`.
- [ ] `uv run mship _session-context` from the workspace root (no active task) prints the notice; with a task active prints nothing.

Then `mship phase review` → `mship finish --require-tests --body-file <body>` (PR body: the 4 mechanisms, the PATH-bug finding, escape hatch; `Refs MOS-189`, `Closes #216`).

---

## Self-Review (by plan author)

**Spec coverage:** ac1 (resolve + fail-closed enforcing / no-op advisory) → Task 2. ac2 (existing commit gate still holds + now fires) → Task 2 (resolution) + existing code (regression covered by current tests; Task 3 adds bypass without breaking it). ac3 (pre-push inventory + `_check-push` semantics) → Task 2 + Task 3. ac4 (MSHIP_BYPASS_GATE allow+log for commit & push) → Task 1 (`record_bypass`) + Task 3. ac5 (`_session-context` notice) → Task 1 (`no_task_notice`) + Task 3. ac6 (`init` installs SessionStart idempotently, preserves existing) → Task 4 + Task 5. ✓

**Placeholder scan:** none — every code step is complete. Two "match the exact schema" notes (state.yaml fixture in Tasks 1/3) are explicit instructions to verify field names against `state.py`, not placeholders for missing logic.

**Type consistency:** `resolve_bypass() -> (bool, str)`, `record_bypass(workspace_root, *, op, branch, reason)`, `no_task_notice(cwd) -> str | None`, `NO_TASK_NOTICE`, builder signature `(_mship_bin: str) -> str` for every `_HOOKS` body, `install_session_hook(workspace_root) -> str` + `SESSION_COMMAND` — used identically across tasks.
