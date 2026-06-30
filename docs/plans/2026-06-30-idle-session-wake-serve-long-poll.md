# Idle-Session Wake + Serve Long-Poll Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `idle-session-wake-serve-long-poll-for-mailbox-239-slice-2` (approved). Slice 2 of issue #239 (builds on the merged slice-1 Stop-hook drain). Open question q1 answered: the relay idle-read timeout is "probably 30s" — keep the serve long-poll cap at 30s and verify against the real relay during run/manual testing.

**Goal:** A shared poll-until-change primitive powering an agent-side `mship inbox wait` long-poll (so an idle session wakes on a new phone message) and a serve-side `GET /threads?wait=1&since=` long-poll (so the phone sees an agent reply quickly).

**Architecture:** A pure `changed_since(threads, since) -> (changed, cursor)` in a new `core/message_wait.py`, wrapped by a sync `wait_for_change` loop (the CLI engine, with injected clock/sleep for tests) and, on the serve side, a small async loop using `asyncio.sleep`. The cursor is an ephemeral timestamp the caller threads through (no persisted file). A `receiving-messages` skill + a SessionStart nudge document the arm/re-arm protocol.

**Tech Stack:** Python 3.14, typer, FastAPI + uvicorn (serve), pytest + `typer.testing.CliRunner` + FastAPI `TestClient`. Run tests with `uv run pytest`. `Thread.updated_at` / `Message.created_at` are timezone-aware UTC datetimes.

---

## File Structure

- **Create** `src/mship/core/message_wait.py` — `changed_since` (pure), `WaitResult` dataclass, `wait_for_change` (sync loop). One clear responsibility: "what changed since a cursor, and block until something does."
- **Modify** `src/mship/cli/message.py` — add the `wait` command alongside `inbox`/`reply`/`messages`.
- **Modify** `src/mship/core/serve.py` — extend `GET /threads` with optional `wait`/`since`/`timeout` long-poll (async); extract a shared `_summaries` projection.
- **Create** `src/mship/skills/receiving-messages/SKILL.md` — the arm/re-arm protocol doc.
- **Modify** `src/mship/core/gate.py` + `src/mship/cli/internal.py` — a `messaging_notice` nudge emitted by the `_session-context` SessionStart hook.
- **Create tests:** `tests/core/test_message_wait.py`, `tests/cli/test_inbox_wait.py`, `tests/core/test_serve_threads_wait.py`, `tests/core/test_messaging_notice.py`.

---

<!-- mship:task id=1 -->
### Task 1: Core `changed_since` + `wait_for_change`

**Files:**
- Create: `src/mship/core/message_wait.py`
- Test: `tests/core/test_message_wait.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_message_wait.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mship.core.message import Message, Thread
from mship.core.message_wait import changed_since, wait_for_change, WaitResult

T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _thread(tid: str, updated: datetime, role: str = "human") -> Thread:
    return Thread(
        id=tid, subject=tid, created_at=updated, updated_at=updated,
        messages=[Message(id=f"{tid}-m", thread_id=tid, role=role, text="x", created_at=updated)],
    )


def test_changed_since_filters_newer_and_reports_cursor():
    threads = [_thread("a", T0), _thread("b", T0 + timedelta(seconds=5))]
    changed, cursor = changed_since(threads, T0)
    assert [t.id for t in changed] == ["b"]          # only strictly-newer
    assert cursor == T0 + timedelta(seconds=5)        # high-water mark


def test_changed_since_empty_when_nothing_newer():
    threads = [_thread("a", T0)]
    changed, cursor = changed_since(threads, T0 + timedelta(seconds=10))
    assert changed == []
    assert cursor == T0 + timedelta(seconds=10)       # never goes backwards


def test_changed_since_empty_store():
    changed, cursor = changed_since([], T0)
    assert changed == [] and cursor == T0


def test_wait_returns_when_a_hit_appears_on_a_later_poll():
    # load_fn returns nothing twice, then an awaiting thread — no real sleeping.
    polls = [[], [], [_thread("a", T0 + timedelta(seconds=1))]]
    calls = {"n": 0}
    def load_fn():
        i = min(calls["n"], len(polls) - 1); calls["n"] += 1; return polls[i]
    clock = {"t": 0.0}
    def now_fn(): return clock["t"]
    def sleep_fn(d): clock["t"] += d
    res = wait_for_change(load_fn, since=T0, timeout=100.0,
                          now_fn=now_fn, sleep_fn=sleep_fn, interval=1.0)
    assert isinstance(res, WaitResult)
    assert res.timed_out is False
    assert [t.id for t in res.threads] == ["a"]
    assert res.cursor == T0 + timedelta(seconds=1)


def test_wait_times_out_with_empty_result():
    clock = {"t": 0.0}
    res = wait_for_change(lambda: [], since=T0, timeout=3.0,
                          now_fn=lambda: clock["t"],
                          sleep_fn=lambda d: clock.__setitem__("t", clock["t"] + d),
                          interval=1.0)
    assert res.timed_out is True
    assert res.threads == []
    assert res.cursor == T0


def test_wait_predicate_filters_hits_but_cursor_still_advances():
    # An agent-role change bumps updated_at but is NOT a hit (predicate=awaiting).
    agent = _thread("a", T0 + timedelta(seconds=1), role="agent")
    clock = {"t": 0.0}
    res = wait_for_change(lambda: [agent], since=T0, timeout=2.0,
                          predicate=lambda t: t.awaiting_reply,
                          now_fn=lambda: clock["t"],
                          sleep_fn=lambda d: clock.__setitem__("t", clock["t"] + d),
                          interval=1.0)
    assert res.timed_out is True            # agent message is not a hit
    assert res.threads == []
    assert res.cursor == T0 + timedelta(seconds=1)   # cursor advanced past it
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/core/test_message_wait.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.message_wait'`.

- [ ] **Step 3: Write `src/mship/core/message_wait.py`**

```python
"""Poll-until-change over the message mailbox: the shared primitive behind the
agent-side `mship inbox wait` long-poll and the serve-side `GET /threads?wait=1`.

`changed_since` is pure (the tested core). `wait_for_change` is a thin sync loop
with injectable clock/sleep so tests never sleep for real. The cursor is an
ephemeral high-water timestamp the caller threads through — there is no on-disk
cursor. See spec idle-session-wake-serve-long-poll-for-mailbox-239-slice-2.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


def changed_since(threads, since: datetime):
    """Return (changed, cursor): threads whose updated_at is strictly after
    `since`, and the high-water cursor = max(updated_at across all threads, since)
    (so the cursor never moves backwards)."""
    changed = [t for t in threads if t.updated_at > since]
    cursor = max([since, *(t.updated_at for t in threads)])
    return changed, cursor


@dataclass(frozen=True)
class WaitResult:
    threads: list
    cursor: datetime
    timed_out: bool


def wait_for_change(
    load_fn: Callable[[], list],
    since: datetime,
    timeout: float,
    *,
    predicate: Callable[[object], bool] | None = None,
    now_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    interval: float = 1.0,
) -> WaitResult:
    """Block until `load_fn()` yields a thread changed since `since` that also
    satisfies `predicate` (default: any change), or `timeout` seconds elapse.
    The cursor always advances to the latest seen updated_at, even on timeout."""
    deadline = now_fn() + timeout
    cursor = since
    while True:
        changed, cursor = changed_since(load_fn(), since)
        hits = [t for t in changed if predicate(t)] if predicate else changed
        if hits:
            return WaitResult(threads=hits, cursor=cursor, timed_out=False)
        remaining = deadline - now_fn()
        if remaining <= 0:
            return WaitResult(threads=[], cursor=cursor, timed_out=True)
        sleep_fn(min(interval, remaining))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/core/test_message_wait.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit + journal**

```bash
git add src/mship/core/message_wait.py tests/core/test_message_wait.py
git commit -m "feat(inbox): changed_since + wait_for_change poll primitive"
mship journal "message_wait core (changed_since + wait_for_change) + tests passing" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2: CLI `mship inbox wait`

**Files:**
- Modify: `src/mship/cli/message.py` (add a `wait` command inside `register`; reuse the existing `_store()` helper)
- Test: `tests/cli/test_inbox_wait.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/cli/test_inbox_wait.py
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.message_store import MessageStore

runner = CliRunner()


def _bootstrap(tmp_path: Path) -> tuple[Path, Path, MessageStore]:
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"; cfg.write_text("workspace: t\nrepos: {}\n")
    return cfg, state_dir, MessageStore(state_dir / "messages")


def _override(cfg, state_dir):
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg); container.state_dir.override(state_dir)


def _reset():
    container.config_path.reset_override(); container.state_dir.reset_override()
    container.config.reset_override(); container.config.reset()
    container.state_manager.reset_override(); container.state_manager.reset()
    container.log_manager.reset()


PAST = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def test_wait_returns_awaiting_thread_immediately_when_newer_than_since(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    store.create_thread("idea", "shape this", datetime.now(timezone.utc))  # awaiting (human)
    _override(cfg, state_dir)
    try:
        # --since in the past => the existing thread counts as new => first poll hits.
        result = runner.invoke(app, ["inbox", "wait", "--since", PAST, "--timeout", "5"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["timed_out"] is False
        assert payload["threads"][0]["pending"] == "shape this"
        assert "cursor" in payload
    finally:
        _reset()


def test_wait_times_out_when_no_new_message(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["inbox", "wait", "--timeout", "0.1"])  # default since=now
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["timed_out"] is True
        assert payload["threads"] == []
    finally:
        _reset()


def test_wait_ignores_agent_reply(tmp_path: Path):
    cfg, state_dir, store = _bootstrap(tmp_path)
    t = store.create_thread("s", "q", datetime.now(timezone.utc))
    store.append(t.id, "agent", "answered", datetime.now(timezone.utc))  # latest = agent
    _override(cfg, state_dir)
    try:
        # Even with --since in the past, an agent-latest thread is not awaiting => no hit.
        result = runner.invoke(app, ["inbox", "wait", "--since", PAST, "--timeout", "0.1"])
        payload = json.loads(result.output)
        assert payload["timed_out"] is True
        assert payload["threads"] == []
    finally:
        _reset()


def test_wait_outside_workspace_errors(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    container.config_path.reset_override(); container.state_dir.reset_override()
    container.config.reset_override(); container.config.reset()
    container.state_manager.reset_override(); container.state_manager.reset()
    try:
        result = runner.invoke(app, ["inbox", "wait", "--timeout", "0.1"])
        assert result.exit_code != 0  # required container -> clear error outside a workspace
    finally:
        _reset()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/cli/test_inbox_wait.py -q`
Expected: FAIL — `inbox` has no `wait` subcommand (typer "No such command 'wait'", exit 2).

Note: today `inbox` is a single `@parent.command()`, so `mship inbox wait` isn't a subcommand. Step 3 converts `inbox` into a small Typer sub-app: a callback with `invoke_without_command=True` preserves the existing `mship inbox` (list awaiting) behavior, and `wait` is added as a subcommand. The existing `mship inbox` tests in `tests/cli/test_message.py` (which invoke `["inbox"]`) must keep passing — the callback runs the same listing logic.

- [ ] **Step 3: Implement in `src/mship/cli/message.py`**

Convert the standalone `inbox` command into a small Typer group with two subcommands — `mship inbox` (list, the existing behavior, as the group callback's no-subcommand path) and `mship inbox wait`. Concretely, replace the `@parent.command() def inbox(): ...` block with:

```python
    inbox_app = typer.Typer(help="Inspect and wait on the message inbox.")
    parent.add_typer(inbox_app, name="inbox")

    def _print_awaiting() -> None:
        store = _store()
        out = [
            {"id": t.id, "subject": t.subject,
             "pending": (t.messages[-1].text if t.messages else ""),
             "updated_at": t.updated_at.isoformat()}
            for t in store.list() if t.awaiting_reply
        ]
        if sys.stdout.isatty():
            if not out:
                typer.echo("(inbox empty)")
            for o in out:
                typer.echo(f"{o['id']}  {o['subject']}\n  > {o['pending']}")
        else:
            typer.echo(json.dumps(out))

    @inbox_app.callback(invoke_without_command=True)
    def inbox(ctx: typer.Context) -> None:
        """List threads awaiting an agent reply (latest message is from a human)."""
        if ctx.invoked_subcommand is None:
            _print_awaiting()

    @inbox_app.command("wait")
    def inbox_wait(
        since: str = typer.Option(None, "--since", help="ISO timestamp; only messages after it count (default: now)."),
        timeout: float = typer.Option(50.0, "--timeout", help="Max seconds to block before returning timed_out."),
    ) -> None:
        """Block until a new awaiting (human) message arrives, or timeout. JSON only."""
        from mship.core.message_wait import wait_for_change
        store = _store()
        since_dt = (
            datetime.fromisoformat(since) if since else datetime.now(timezone.utc)
        )
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        res = wait_for_change(
            store.list, since_dt, timeout,
            predicate=lambda t: t.awaiting_reply,
        )
        out = {
            "threads": [
                {"id": t.id, "subject": t.subject,
                 "pending": (t.messages[-1].text if t.messages else ""),
                 "updated_at": t.updated_at.isoformat()}
                for t in res.threads
            ],
            "cursor": res.cursor.isoformat(),
            "timed_out": res.timed_out,
        }
        typer.echo(json.dumps(out))
```

`_store()`, `sys`, `json`, `datetime`, `timezone`, and `typer` are already imported/defined at the top of `message.py`. The existing `reply` and `messages` commands stay as they are (`@parent.command()`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/cli/test_inbox_wait.py -q`
Expected: PASS (4 passed). Also run the existing inbox test to confirm `mship inbox` still lists awaiting threads:
`uv run pytest -q -k "inbox or message"`

- [ ] **Step 5: Commit + journal**

```bash
git add src/mship/cli/message.py tests/cli/test_inbox_wait.py
git commit -m "feat(inbox): mship inbox wait long-poll for new human messages"
mship journal "mship inbox wait (awaiting-projected, --since cursor) + tests" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3: Serve `GET /threads?wait=1&since=&timeout=`

**Files:**
- Modify: `src/mship/core/serve.py` (extend the `GET /threads` handler at ~line 317; extract a `_summaries` helper)
- Test: `tests/core/test_serve_threads_wait.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_serve_threads_wait.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.message_store import MessageStore
from mship.core.state import StateManager


def _client(tmp_path: Path, auth_token=None) -> tuple[TestClient, MessageStore]:
    # Mirror the existing tests/core/test_serve.py::_app construction (create_app
    # requires specs_dir/state_manager/log_manager/workspace_root/workspace_name).
    app = create_app(
        specs_dir=tmp_path / "specs",
        state_manager=StateManager(tmp_path / ".mothership"),
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
        auth_token=auth_token,
    )
    store = MessageStore(tmp_path / ".mothership" / "messages")
    return TestClient(app), store


PAST = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def test_plain_get_threads_unchanged(tmp_path: Path):
    client, store = _client(tmp_path)
    store.create_thread("s", "hi", datetime.now(timezone.utc))
    r = client.get("/threads")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and body[0]["subject"] == "s"  # list shape preserved


def test_wait_returns_changed_when_newer_than_since(tmp_path: Path):
    client, store = _client(tmp_path)
    store.create_thread("s", "hi", datetime.now(timezone.utc))
    r = client.get("/threads", params={"wait": 1, "since": PAST, "timeout": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["timed_out"] is False
    assert body["threads"][0]["subject"] == "s"
    assert "cursor" in body


def test_wait_times_out_with_empty_list(tmp_path: Path):
    client, _ = _client(tmp_path)
    r = client.get("/threads", params={"wait": 1, "timeout": 0.1})  # since defaults to now
    assert r.status_code == 200
    body = r.json()
    assert body["timed_out"] is True
    assert body["threads"] == []


def test_wait_requires_auth(tmp_path: Path):
    client, _ = _client(tmp_path, auth_token="secret")
    r = client.get("/threads", params={"wait": 1, "timeout": 0.1})
    assert r.status_code == 401
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/core/test_serve_threads_wait.py -q`
Expected: FAIL — the wait params are ignored today, so `test_wait_returns_changed_when_newer_than_since` gets a list (no `["timed_out"]`) and the timeout test fails on the dict access.

- [ ] **Step 3: Implement in `src/mship/core/serve.py`**

Add imports near the top of the file (with the other stdlib imports):
```python
import asyncio
import time as _time
from typing import Optional
```

Replace the existing `@app.get("/threads")` / `def list_threads():` block (lines ~317-328) with a `_summaries` helper + an async handler:
```python
    def _summaries(threads):
        return [
            {
                "id": t.id, "subject": t.subject,
                "updated_at": t.updated_at.isoformat(),
                "awaiting_reply": t.awaiting_reply,
                "last_message": (t.messages[-1].text[:120] if t.messages else ""),
                "message_count": len(t.messages),
            }
            for t in threads
        ]

    @app.get("/threads")
    async def list_threads(wait: int = 0, since: Optional[str] = None, timeout: float = 25.0):
        if not wait:
            return _summaries(msgs.list())
        from mship.core.message_wait import changed_since
        timeout = max(0.0, min(timeout, 30.0))  # cap for the relay idle-read timeout
        since_dt = datetime.fromisoformat(since) if since else datetime.now(timezone.utc)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        interval = 1.0
        deadline = _time.monotonic() + timeout
        while True:
            changed, cursor = changed_since(msgs.list(), since_dt)
            if changed:
                return {"threads": _summaries(changed), "cursor": cursor.isoformat(), "timed_out": False}
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return {"threads": [], "cursor": cursor.isoformat(), "timed_out": True}
            await asyncio.sleep(min(interval, remaining))
```

The `GET /threads/{thread_id}` handler below it is unchanged. Auth is already applied app-wide (the `dependencies=[Depends(_make_auth_dependency(...))]` at app creation), so the async endpoint inherits the bearer requirement automatically.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/core/test_serve_threads_wait.py -q`
Expected: PASS (4 passed). Also run the existing serve tests to confirm no regression:
`uv run pytest -q -k serve`

- [ ] **Step 5: Commit + journal**

```bash
git add src/mship/core/serve.py tests/core/test_serve_threads_wait.py
git commit -m "feat(serve): GET /threads?wait=1 long-poll (async, 30s cap)"
mship journal "serve GET /threads?wait long-poll + tests; plain GET /threads unchanged" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=4 -->
### Task 4: `receiving-messages` skill + SessionStart nudge

**Files:**
- Create: `src/mship/skills/receiving-messages/SKILL.md`
- Modify: `src/mship/core/gate.py` (add `messaging_notice`), `src/mship/cli/internal.py` (`_session-context` prints it)
- Test: `tests/core/test_messaging_notice.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_messaging_notice.py
from __future__ import annotations

from pathlib import Path

from mship.core.gate import messaging_notice


def test_messaging_notice_in_workspace(tmp_path: Path):
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    notice = messaging_notice(tmp_path)
    assert notice is not None
    assert "inbox wait" in notice
    assert "receiving-messages" in notice


def test_messaging_notice_outside_workspace_is_none(tmp_path: Path):
    assert messaging_notice(tmp_path) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/core/test_messaging_notice.py -q`
Expected: FAIL — `ImportError: cannot import name 'messaging_notice'`.

- [ ] **Step 3: Add `messaging_notice` to `src/mship/core/gate.py`**

Add after `no_task_notice`:
```python
def messaging_notice(cwd: Path) -> str | None:
    """A one-line nudge for the SessionStart hook: keep a background
    `mship inbox wait` armed so this session wakes on a new phone message.
    Returns None outside a workspace (fail-open advisory context)."""
    try:
        from mship.core.config import ConfigLoader
        try:
            ConfigLoader.discover(cwd)
        except FileNotFoundError:
            return None
    except Exception:
        return None
    return (
        "Phone messages may arrive mid-session. To answer them while idle, keep a "
        "background `mship inbox wait` armed and re-arm after each reply — see the "
        "`receiving-messages` skill. (Messages mid-turn are also caught by the Stop hook.)"
    )
```

- [ ] **Step 4: Wire it into `_session-context` in `src/mship/cli/internal.py`**

In the `_session-context` command, after printing `no_task_notice`, also print the messaging notice:
```python
    @app.command(name="_session-context", hidden=True)
    def session_context():
        """Print the no-active-task notice + messaging nudge for the SessionStart hook."""
        import sys
        from mship.core.gate import no_task_notice, messaging_notice
        for fn in (no_task_notice, messaging_notice):
            text = fn(Path.cwd())
            if text:
                sys.stdout.write(text + "\n")
        raise typer.Exit(code=0)
```

- [ ] **Step 5: Create `src/mship/skills/receiving-messages/SKILL.md`**

```markdown
---
name: receiving-messages
description: Use to receive and answer durable phone messages (the mship mailbox) while a session is idle — keep a background `mship inbox wait` armed and re-arm after each reply.
---

# Receiving Messages

The phone sends durable messages to this workspace's mailbox (`mship inbox`). Two
mechanisms surface them to a live agent (one serve + one agent per workspace):

- **Mid-turn:** the `Stop` hook (`mship _drain`) drains the inbox at each turn
  boundary automatically — you don't have to do anything.
- **While idle:** keep a long-poll armed so you wake when a message lands.

## The idle arm/re-arm loop

1. When you finish your work and would otherwise go idle, run **in the
   background**: `mship inbox wait --timeout 50` (like backgrounding a test run).
   It blocks until a new *human* message arrives (or it times out), then returns
   JSON `{threads, cursor, timed_out}` and your harness re-invokes you.
2. On wake with `threads`, answer each and clear it:
   `mship reply <thread-id> "<your answer>"`.
3. **Re-arm** with the returned cursor: `mship inbox wait --since <cursor> --timeout 50`.
   The `--since` cursor means you never re-wake for a message you already handled
   (or for your own reply).
4. On `timed_out: true`, just re-arm again.

Never spawn a new agent / `claude -p` — this is all in your existing session.
```

- [ ] **Step 6: Run the test + any skill-registry test**

Run: `uv run pytest tests/core/test_messaging_notice.py -q && uv run pytest -q -k "skill or session_context or internal"`
Expected: PASS (the new skill's frontmatter is valid `name` + `description`, matching the other `src/mship/skills/*/SKILL.md`).

- [ ] **Step 7: Commit + journal**

```bash
git add src/mship/core/gate.py src/mship/cli/internal.py src/mship/skills/receiving-messages/SKILL.md tests/core/test_messaging_notice.py
git commit -m "feat(inbox): receiving-messages skill + SessionStart wait nudge"
mship journal "receiving-messages skill + messaging_notice SessionStart nudge + tests" --action committed
```
<!-- /mship:task -->

---

## Final verification (after all tasks)

- [ ] Full suite: `mship test`. Expected: all pass (no regressions to message/serve/internal suites).
- [ ] Manual smoke from inside this worktree (no real long wait):
  ```bash
  uv run mship inbox wait --timeout 0.2   # empty inbox -> {"threads": [], "cursor": ..., "timed_out": true}
  ```
- [ ] Note for run-phase: confirm the serve `?wait=1` long-poll survives the relay/Caddy/sish idle-read timeout (q1: "probably 30s"); if the relay cuts idle connections sooner, lower the `timeout` the phone passes (the server already caps at 30s).
