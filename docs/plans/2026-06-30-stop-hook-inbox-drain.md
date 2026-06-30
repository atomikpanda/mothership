# Stop-Hook Inbox Drain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `stop-hook-inbox-drain` (approved). Slice 1 of issue #239. Idle-session wake (`mship inbox wait` + read cursor) is Spec 2 and explicitly out of scope here.

**Goal:** A Claude Code `Stop` hook that drains this workspace's message inbox at each turn boundary, so a live agent answers pending phone messages with no manual `mship inbox`.

**Architecture:** A new hidden `mship _drain` reads the Stop event JSON from stdin and inspects the cwd-resolved workspace's `MessageStore`; if threads await an agent it blocks the stop and injects them, else it allows the stop (also allows on `stop_hook_active` for loop safety, and on any error to fail open). An `install_stop_hook` (third caller of the existing `_install_hook_entry`) wires it into `.claude/settings.json` via `mship init --install-hooks`.

**Tech Stack:** Python 3.14, typer, pytest + `typer.testing.CliRunner` (Click 8.3.2 — stdout in `result.output`, stderr in `result.stderr`). Run tests with `uv run pytest`.

**Stop-hook contract:** a Claude Code `Stop` hook reads event JSON on stdin (`{stop_hook_active: bool, ...}`). To BLOCK the turn from ending, it prints `{"decision": "block", "reason": "<text>"}` to stdout and exits 0 (the reason is shown to the model). To ALLOW the stop, it exits 0 with no output. `Stop` hook entries (like `SessionStart`) carry no `matcher`.

---

## File Structure

- **Modify** `src/mship/cli/internal.py` — add a module-level `_format_drain_reason(threads)` helper and a hidden `_drain` command inside the existing `register(app, get_container)` (mirrors `_guard-edit`'s stdin-read + fail-open shape).
- **Modify** `src/mship/core/claude_settings.py` — add `DRAIN_COMMAND` + `install_stop_hook` (third caller of `_install_hook_entry`).
- **Modify** `src/mship/cli/init.py` — install the Stop hook in `_install_agent_hooks_with_output` alongside the SessionStart + PreToolUse hooks.
- **Create tests:** `tests/cli/test_drain.py`, `tests/core/test_claude_settings_stop.py`, `tests/cli/test_init_stop_hook.py`.

---

<!-- mship:task id=1 -->
### Task 1: `mship _drain` hidden command

**Files:**
- Modify: `src/mship/cli/internal.py` (add `_format_drain_reason` at module level near the top, after the imports; add the `_drain` command inside `register(app, get_container)`, next to `_guard-edit`)
- Test: `tests/cli/test_drain.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/cli/test_drain.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.message_store import MessageStore

runner = CliRunner()


def _bootstrap(tmp_path: Path) -> tuple[Path, Path, MessageStore]:
    """Workspace with a MessageStore; returns (cfg, state_dir, store)."""
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    store = MessageStore(state_dir / "messages")
    return cfg, state_dir, store


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


def _event(stop_hook_active: bool = False) -> str:
    return json.dumps({"hook_event_name": "Stop", "stop_hook_active": stop_hook_active})


def test_blocks_when_threads_await(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    now = datetime.now(timezone.utc)
    store.create_thread("first idea", "shape this into a spec", now)
    store.create_thread("second", "and answer this", now)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_drain"], input=_event())
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["decision"] == "block"
        assert "shape this into a spec" in payload["reason"]
        assert "and answer this" in payload["reason"]
        assert "mship reply" in payload["reason"]
    finally:
        _reset()


def test_allows_when_inbox_empty(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_drain"], input=_event())
        assert result.exit_code == 0
        assert '"decision"' not in result.output
    finally:
        _reset()


def test_allows_when_thread_already_answered(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    now = datetime.now(timezone.utc)
    t = store.create_thread("s", "q", now)
    store.append(t.id, "agent", "answered", now)  # latest role == agent -> not awaiting
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_drain"], input=_event())
        assert result.exit_code == 0
        assert '"decision"' not in result.output
    finally:
        _reset()


def test_allows_when_stop_hook_active(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    store.create_thread("s", "still pending", datetime.now(timezone.utc))
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_drain"], input=_event(stop_hook_active=True))
        assert result.exit_code == 0
        assert '"decision"' not in result.output  # loop safety: never block twice
    finally:
        _reset()


def test_malformed_stdin_fails_open(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    store.create_thread("s", "pending", datetime.now(timezone.utc))
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_drain"], input="not json{{")
        assert result.exit_code == 0
        assert '"decision"' not in result.output
    finally:
        _reset()


def test_outside_workspace_fails_open(tmp_path: Path):
    # No container override -> get_container(required=False) returns None.
    result = runner.invoke(app, ["_drain"], input=_event())
    assert result.exit_code == 0
    assert '"decision"' not in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/cli/test_drain.py -q`
Expected: FAIL — `_drain` is not a registered command (typer "No such command", exit code 2), so the allow-case assertions on exit_code 0 fail and the block case can't parse output.

- [ ] **Step 3: Add the `_format_drain_reason` helper to `src/mship/cli/internal.py`**

Add at module level (after the existing imports / near `_staged_source_paths`):

```python
def _format_drain_reason(threads) -> str:
    """Render awaiting threads as a Stop-hook block reason instructing the agent
    to answer each and clear it with `mship reply`."""
    n = len(threads)
    lines = [
        f"{n} message{'s' if n != 1 else ''} waiting in your inbox. Answer each, "
        f"then post your answer with `mship reply <thread-id> \"<text>\"` "
        f"(replying clears the thread):",
        "",
    ]
    for t in threads:
        pending = t.messages[-1].text if t.messages else ""
        lines.append(f"- thread {t.id} ({t.subject}): {pending}")
    return "\n".join(lines)
```

- [ ] **Step 4: Add the `_drain` command inside `register(app, get_container)` in `src/mship/cli/internal.py`** (place it next to `_guard-edit`)

```python
    @app.command(name="_drain", hidden=True)
    def _drain():
        """Stop hook: drain this workspace's message inbox at a turn boundary.
        Reads the Claude Code Stop event JSON from stdin. If threads await an
        agent reply, blocks the stop and injects them; otherwise allows the stop.
        Also allows on stop_hook_active (loop safety) and on ANY error (fail
        open) — a messaging glitch must never trap the agent."""
        from mship.core.message_store import MessageStore

        try:
            raw = sys.stdin.read()
            event = json.loads(raw) if raw.strip() else {}
            if event.get("stop_hook_active"):
                raise typer.Exit(code=0)  # already a stop-hook continuation; don't loop
            container = get_container(required=False)
            if container is None:
                raise typer.Exit(code=0)
            store = MessageStore(Path(container.state_dir()) / "messages")
            awaiting = [t for t in store.list() if t.awaiting_reply]
            if not awaiting:
                raise typer.Exit(code=0)
            reason = _format_drain_reason(awaiting)
        except typer.Exit:
            raise
        except Exception:
            raise typer.Exit(code=0)  # fail open
        print(json.dumps({"decision": "block", "reason": reason}))
        raise typer.Exit(code=0)
```

(`json`, `sys`, and `Path` are already imported at the top of `internal.py`.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/cli/test_drain.py -q`
Expected: PASS (6 passed).

- [ ] **Step 6: Commit + journal**

```bash
git add src/mship/cli/internal.py tests/cli/test_drain.py
git commit -m "feat(inbox): mship _drain Stop-hook command (block-when-awaiting, fail-open)"
mship journal "_drain Stop-hook command + tests passing" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2: `install_stop_hook`

**Files:**
- Modify: `src/mship/core/claude_settings.py` (add `DRAIN_COMMAND` constant + `install_stop_hook`)
- Test: `tests/core/test_claude_settings_stop.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_claude_settings_stop.py
from __future__ import annotations

import json
from pathlib import Path

from mship.core.claude_settings import (
    install_stop_hook, install_session_hook, install_pretooluse_guard_hook,
    DRAIN_COMMAND,
)


def _settings(ws: Path) -> dict:
    return json.loads((ws / ".claude" / "settings.json").read_text())


def test_installs_stop_hook_into_fresh_settings(tmp_path: Path):
    assert install_stop_hook(tmp_path) == "installed"
    stop = _settings(tmp_path)["hooks"]["Stop"]
    assert stop[0]["hooks"][0]["command"] == DRAIN_COMMAND
    assert "matcher" not in stop[0]  # Stop hooks carry no matcher


def test_install_stop_is_idempotent(tmp_path: Path):
    assert install_stop_hook(tmp_path) == "installed"
    assert install_stop_hook(tmp_path) == "up to date"
    assert len(_settings(tmp_path)["hooks"]["Stop"]) == 1


def test_stop_preserves_other_hooks(tmp_path: Path):
    install_session_hook(tmp_path)
    install_pretooluse_guard_hook(tmp_path)
    install_stop_hook(tmp_path)
    hooks = _settings(tmp_path)["hooks"]
    assert "SessionStart" in hooks and "PreToolUse" in hooks and "Stop" in hooks


def test_stop_tolerates_malformed_settings(tmp_path: Path):
    cdir = tmp_path / ".claude"; cdir.mkdir()
    (cdir / "settings.json").write_text("{not json")
    assert "skipped" in install_stop_hook(tmp_path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/core/test_claude_settings_stop.py -q`
Expected: FAIL — `ImportError: cannot import name 'install_stop_hook'`.

- [ ] **Step 3: Add to `src/mship/core/claude_settings.py`**

Add the constant next to the others (after `GUARD_MATCHER`):
```python
DRAIN_COMMAND = "mship _drain"
```

Add the installer after `install_pretooluse_guard_hook`:
```python
def install_stop_hook(workspace_root: Path) -> str:
    """Idempotently add a Stop hook running `mship _drain` (drains the message
    inbox at each turn boundary). Stop hooks carry no matcher."""
    return _install_hook_entry(
        workspace_root, "Stop",
        {"hooks": [{"type": "command", "command": DRAIN_COMMAND}]},
        DRAIN_COMMAND,
    )
```

- [ ] **Step 4: Run the new tests + existing claude_settings tests**

Run: `uv run pytest tests/core/test_claude_settings_stop.py -q && uv run pytest -q -k claude_settings`
Expected: PASS (existing SessionStart + guard installer tests still green; the shared `_install_hook_entry` is unchanged).

- [ ] **Step 5: Commit + journal**

```bash
git add src/mship/core/claude_settings.py tests/core/test_claude_settings_stop.py
git commit -m "feat(inbox): install_stop_hook for the _drain Stop hook"
mship journal "install_stop_hook + tests; other hook installers unaffected" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3: Wire the Stop hook into `mship init --install-hooks`

**Files:**
- Modify: `src/mship/cli/init.py` (import `install_stop_hook`; install it in `_install_agent_hooks_with_output`)
- Test: `tests/cli/test_init_stop_hook.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/cli/test_init_stop_hook.py
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def test_install_hooks_installs_stop_hook(tmp_path: Path):
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["init", "--install-hooks"])
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert "Stop" in data["hooks"]
        assert any(
            h.get("command") == "mship _drain"
            for e in data["hooks"]["Stop"]
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

Run: `uv run pytest tests/cli/test_init_stop_hook.py -q`
Expected: FAIL — `KeyError: 'Stop'` (only SessionStart + PreToolUse are installed today).

- [ ] **Step 3: Update `src/mship/cli/init.py`**

Change the import line to add `install_stop_hook`:
```python
from mship.core.claude_settings import install_session_hook, install_pretooluse_guard_hook, install_stop_hook
```

Append a third install block to `_install_agent_hooks_with_output` (after the PreToolUse block):
```python
    try:
        output.success(f"Stop hook @ {settings}: {install_stop_hook(ws_root)}")
    except Exception as e:
        output.warning(f"Stop hook install skipped: {e}")
```

- [ ] **Step 4: Run the test + existing init tests**

Run: `uv run pytest tests/cli/test_init_stop_hook.py -q && uv run pytest -q -k init`
Expected: PASS (existing init/SessionStart/PreToolUse tests still green; the Stop hook now installs too).

- [ ] **Step 5: Commit + journal**

```bash
git add src/mship/cli/init.py tests/cli/test_init_stop_hook.py
git commit -m "feat(inbox): install the Stop drain hook in init --install-hooks"
mship journal "init --install-hooks now installs the Stop drain hook" --action committed
```
<!-- /mship:task -->

---

## Final verification (after all tasks)

- [ ] Full suite: `mship test`. Expected: all pass (no regressions to the message/hook/init suites).
- [ ] Manual smoke from inside this worktree:
  ```bash
  # Seed an awaiting thread in THIS workspace, then drain.
  uv run mship serve --help >/dev/null  # (sanity)
  printf '{"stop_hook_active": false}' | uv run mship _drain; echo "exit=$?"
  ```
  With no awaiting messages, expect empty output + `exit=0`. (A full block smoke would require POSTing a message via `mship serve`; the unit tests cover the block path.)
