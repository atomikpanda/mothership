# Message mailbox substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** `message-mailbox` (approved) — workspace `specs/2026-06-23-message-mailbox.md`.

**Goal:** A durable, agent-agnostic two-way message mailbox in mothership: a `MessageStore` (file-per-thread, like `SpecStore`), four `mship serve` endpoints (the phone's side), and `mship inbox`/`reply`/`messages` CLI (the agent's side). Store-and-forward; `awaiting_reply` is derived from the latest message's role.

**Architecture:** mothership-only. `MessageStore` mirrors `core/spec_store.py` (atomic temp-file + `Path.replace`, glob list). Serve endpoints added inside `create_app` (builds the store from `workspace_root`). CLI is a new `cli/message.py` registered like every other `cli/*` module. Pydantic models, consistent with `Spec`.

**Tech Stack:** Python, FastAPI (serve), Typer (CLI), Pydantic, pytest (`uv run`).

**dev-binary note:** run tests via `uv run pytest …`.

---

<!-- mship:task id=1 -->
### Task 1: Models + MessageStore

**Files:**
- Create: `src/mship/core/message.py`
- Create: `src/mship/core/message_store.py`
- Test: `tests/core/test_message_store.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_message_store.py`:
```python
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from mship.core.message_store import MessageStore


def _store(tmp_path: Path) -> MessageStore:
    return MessageStore(tmp_path / ".mothership" / "messages")


def test_create_thread_persists_with_first_human_message(tmp_path):
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="Idea", text="build a thing", now=now)
    assert t.subject == "Idea"
    assert [m.role for m in t.messages] == ["human"]
    assert t.messages[0].text == "build a thing"
    assert t.awaiting_reply is True
    # round-trips from disk
    loaded = s.get(t.id)
    assert loaded is not None and loaded.messages[0].text == "build a thing"


def test_append_agent_clears_awaiting_then_human_reraises(tmp_path):
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    s = _store(tmp_path)
    t = s.create_thread(subject="x", text="hi", now=now)
    s.append(t.id, "agent", "drafted it", now + timedelta(minutes=1))
    assert s.get(t.id).awaiting_reply is False
    s.append(t.id, "human", "one more", now + timedelta(minutes=2))
    got = s.get(t.id)
    assert got.awaiting_reply is True
    assert got.updated_at == now + timedelta(minutes=2)
    assert [m.role for m in got.messages] == ["human", "agent", "human"]


def test_append_unknown_thread_raises(tmp_path):
    with pytest.raises(KeyError):
        _store(tmp_path).append("nope", "agent", "x", datetime.now(timezone.utc))


def test_list_sorted_by_updated_desc(tmp_path):
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    s = _store(tmp_path)
    a = s.create_thread(subject="a", text="a", now=now)
    b = s.create_thread(subject="b", text="b", now=now + timedelta(minutes=5))
    assert [t.id for t in s.list()] == [b.id, a.id]


def test_get_missing_is_none(tmp_path):
    assert _store(tmp_path).get("missing") is None


def test_unsafe_thread_id_rejected(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s._path("../escape")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/core/test_message_store.py -q`
Expected: FAIL — `mship.core.message_store` doesn't exist.

- [ ] **Step 3: Implement the models + store**

`src/mship/core/message.py`:
```python
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class Message(BaseModel):
    id: str
    thread_id: str
    role: Literal["human", "agent"]
    text: str
    created_at: datetime


class Thread(BaseModel):
    id: str
    subject: str
    created_at: datetime
    updated_at: datetime
    task_slug: str | None = None
    messages: list[Message] = []

    @property
    def awaiting_reply(self) -> bool:
        """A thread needs an agent iff its latest message is from a human."""
        return bool(self.messages) and self.messages[-1].role == "human"
```

`src/mship/core/message_store.py` (mirrors `spec_store.py`'s atomic write + glob list):
```python
from __future__ import annotations

import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from mship.core.message import Message, Thread


def _new_id(now: datetime) -> str:
    """Sortable, collision-free id: a timestamp prefix + short uuid."""
    return f"{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"


class MessageStore:
    """Filesystem registry for conversation threads: one JSON file per thread."""

    def __init__(self, messages_dir: Path) -> None:
        self._dir = Path(messages_dir)

    def _path(self, thread_id: str) -> Path:
        if (not thread_id or "/" in thread_id or "\\" in thread_id
                or thread_id in (".", "..") or thread_id.startswith(".")):
            raise ValueError(f"unsafe thread id: {thread_id!r}")
        return self._dir / f"{thread_id}.json"

    def save(self, thread: Thread) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(thread.id)
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".json.tmp")
        try:
            with open(fd, "w") as f:
                f.write(thread.model_dump_json(indent=2))
            Path(tmp).replace(path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def get(self, thread_id: str) -> Thread | None:
        path = self._path(thread_id)
        if not path.is_file():
            return None
        return Thread.model_validate_json(path.read_text())

    def list(self) -> list[Thread]:
        if not self._dir.is_dir():
            return []
        threads = [Thread.model_validate_json(p.read_text()) for p in self._dir.glob("*.json")]
        return sorted(threads, key=lambda t: t.updated_at, reverse=True)

    def create_thread(self, subject: str, text: str, now: datetime, task_slug: str | None = None) -> Thread:
        tid = _new_id(now)
        thread = Thread(id=tid, subject=subject, created_at=now, updated_at=now, task_slug=task_slug)
        thread.messages.append(Message(id=_new_id(now), thread_id=tid, role="human", text=text, created_at=now))
        self.save(thread)
        return thread

    def append(self, thread_id: str, role: str, text: str, now: datetime) -> Message:
        thread = self.get(thread_id)
        if thread is None:
            raise KeyError(thread_id)
        msg = Message(id=_new_id(now), thread_id=thread_id, role=role, text=text, created_at=now)
        thread.messages.append(msg)
        thread.updated_at = now
        self.save(thread)
        return msg
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/core/test_message_store.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/message.py src/mship/core/message_store.py tests/core/test_message_store.py
git commit -m "feat(core): MessageStore + Thread/Message models (durable file-per-thread mailbox)"
mship journal "added MessageStore (file-per-thread, atomic) + Thread/Message models; awaiting_reply derived; tests green" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2: serve endpoints

**Files:**
- Modify: `src/mship/core/serve.py`
- Test: `tests/core/test_serve.py` (append)

- [ ] **Step 1: Write the failing tests** — append to `tests/core/test_serve.py` (it has `_app(tmp_path)` → `create_app(...)` with no auth, and uses `TestClient`):

```python
def test_threads_create_append_list_get(tmp_path):
    client = TestClient(_app(tmp_path))

    # create a thread (derives subject from text when omitted)
    r = client.post("/threads", json={"text": "build a thing that does X"})
    assert r.status_code == 200
    thread = r.json()
    tid = thread["id"]
    assert thread["subject"].startswith("build a thing")
    assert [m["role"] for m in thread["messages"]] == ["human"]

    # list shows it, awaiting an agent
    lst = client.get("/threads").json()
    assert any(t["id"] == tid and t["awaiting_reply"] is True for t in lst)

    # append a human message
    r2 = client.post(f"/threads/{tid}/messages", json={"text": "second thought"})
    assert r2.status_code == 200
    assert len(r2.json()["messages"]) == 2

    # get full thread
    full = client.get(f"/threads/{tid}").json()
    assert [m["text"] for m in full["messages"]] == ["build a thing that does X", "second thought"]


def test_threads_404s(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.get("/threads/nope").status_code == 404
    assert client.post("/threads/nope/messages", json={"text": "x"}).status_code == 404


def test_threads_explicit_subject(tmp_path):
    client = TestClient(_app(tmp_path))
    t = client.post("/threads", json={"text": "body", "subject": "My subject"}).json()
    assert t["subject"] == "My subject"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/core/test_serve.py -q -k threads`
Expected: FAIL — endpoints 404 (not defined yet).

- [ ] **Step 3: Implement**

In `src/mship/core/serve.py`, add the two request-body models at module level next to the others (after `class ReasonBody(...)`):
```python
class NewThreadBody(BaseModel):
    text: str
    subject: str | None = None


class NewMessageBody(BaseModel):
    text: str
```
Then, inside `create_app`, just before the final `return app`, add the message endpoints (datetime/timezone are already imported inside the function; HTTPException is in scope):
```python
    # --- message mailbox (phone <-> agent) ---
    from mship.core.message_store import MessageStore

    msgs = MessageStore(workspace_root / ".mothership" / "messages")

    @app.post("/threads")
    def post_thread(body: NewThreadBody):
        now = datetime.now(timezone.utc)
        text = body.text
        subject = body.subject or (text.strip().splitlines()[0][:80] if text.strip() else "(no subject)")
        return msgs.create_thread(subject=subject, text=text, now=now).model_dump(mode="json")

    @app.post("/threads/{thread_id}/messages")
    def post_message(thread_id: str, body: NewMessageBody):
        now = datetime.now(timezone.utc)
        try:
            msgs.append(thread_id, "human", body.text, now)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id!r}")
        return msgs.get(thread_id).model_dump(mode="json")

    @app.get("/threads")
    def list_threads():
        return [
            {
                "id": t.id, "subject": t.subject,
                "updated_at": t.updated_at.isoformat(),
                "awaiting_reply": t.awaiting_reply,
                "last_message": (t.messages[-1].text[:120] if t.messages else ""),
                "message_count": len(t.messages),
            }
            for t in msgs.list()
        ]

    @app.get("/threads/{thread_id}")
    def get_thread(thread_id: str):
        t = msgs.get(thread_id)
        if t is None:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id!r}")
        return t.model_dump(mode="json")
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/core/test_serve.py -q`
Expected: PASS (new thread tests + all existing serve tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/serve.py tests/core/test_serve.py
git commit -m "feat(serve): message mailbox endpoints (POST/GET /threads, messages)"
mship journal "added serve endpoints POST /threads, POST /threads/{id}/messages, GET /threads, GET /threads/{id}; tests green" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3: CLI — inbox / reply / messages

**Files:**
- Create: `src/mship/cli/message.py`
- Modify: `src/mship/cli/__init__.py`
- Test: `tests/cli/test_message.py`

- [ ] **Step 1: Write the failing tests**

`tests/cli/test_message.py` (mirrors `tests/cli/test_serve.py`'s `_configured` fixture; seeds threads via `MessageStore` in the workspace's `.mothership/messages`):
```python
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.message_store import MessageStore

runner = CliRunner()


@pytest.fixture
def _configured(workspace: Path):
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(workspace / "mothership.yaml")
    container.state_dir.override(state_dir)
    yield workspace
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


def _seed(workspace: Path) -> MessageStore:
    return MessageStore(workspace / ".mothership" / "messages")


def test_inbox_lists_only_awaiting(_configured):
    s = _seed(_configured)
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    awaiting = s.create_thread(subject="needs reply", text="please draft", now=now)
    answered = s.create_thread(subject="done", text="hi", now=now)
    s.append(answered.id, "agent", "handled", now)

    result = runner.invoke(app, ["inbox"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)   # CliRunner output is non-TTY -> JSON
    ids = {o["id"] for o in out}
    assert awaiting.id in ids and answered.id not in ids
    assert any(o["pending"] == "please draft" for o in out)


def test_reply_appends_and_clears(_configured):
    s = _seed(_configured)
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    t = s.create_thread(subject="x", text="q", now=now)
    r = runner.invoke(app, ["reply", t.id, "here is the answer"])
    assert r.exit_code == 0, r.output
    got = s.get(t.id)
    assert got.messages[-1].role == "agent"
    assert got.messages[-1].text == "here is the answer"
    assert got.awaiting_reply is False
    # cleared from inbox
    assert json.loads(runner.invoke(app, ["inbox"]).output) == []


def test_reply_unknown_thread_errors(_configured):
    assert runner.invoke(app, ["reply", "nope", "x"]).exit_code != 0


def test_messages_renders_thread(_configured):
    s = _seed(_configured)
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    t = s.create_thread(subject="x", text="first", now=now)
    s.append(t.id, "agent", "second", now)
    out = json.loads(runner.invoke(app, ["messages", t.id]).output)
    assert [m["text"] for m in out["messages"]] == ["first", "second"]
```

> Note: `workspace` is the shared fixture used by `tests/cli/test_serve.py`'s `_configured`. If it's not auto-available, copy the minimal `workspace` fixture from the conftest used there.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/cli/test_message.py -q`
Expected: FAIL — no `inbox`/`reply`/`messages` commands registered.

- [ ] **Step 3: Implement the CLI**

`src/mship/cli/message.py`:
```python
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer

from mship.core.message_store import MessageStore


def register(parent: typer.Typer, get_container) -> None:
    def _store() -> MessageStore:
        container = get_container()
        return MessageStore(Path(container.state_dir()) / "messages")

    @parent.command()
    def inbox() -> None:
        """List threads awaiting an agent reply (latest message is from a human)."""
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

    @parent.command()
    def reply(thread_id: str, text: str) -> None:
        """Post an agent reply to a thread."""
        store = _store()
        try:
            store.append(thread_id, "agent", text, datetime.now(timezone.utc))
        except KeyError:
            typer.echo(f"no thread {thread_id!r}", err=True)
            raise typer.Exit(1)
        typer.echo(f"replied to {thread_id}")

    @parent.command()
    def messages(thread_id: str) -> None:
        """Print a thread's conversation in order."""
        store = _store()
        t = store.get(thread_id)
        if t is None:
            typer.echo(f"no thread {thread_id!r}", err=True)
            raise typer.Exit(1)
        if sys.stdout.isatty():
            for m in t.messages:
                typer.echo(f"[{m.role}] {m.text}")
        else:
            typer.echo(t.model_dump_json())
```

In `src/mship/cli/__init__.py`, add the import alongside the other `_xxx_mod` imports and the registration alongside the other `register(app, get_container)` calls:
```python
from mship.cli import message as _message_mod
...
_message_mod.register(app, get_container)
```
(Match the exact import style used for the sibling modules — e.g. how `_serve_mod` / `_log_mod` are imported.)

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/cli/test_message.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/message.py src/mship/cli/__init__.py tests/cli/test_message.py
git commit -m "feat(cli): mship inbox / reply / messages (agent side of the mailbox)"
mship journal "added mship inbox/reply/messages CLI; agent-agnostic contract over the durable mailbox; tests green" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=4 -->
### Task 4: full verification + phase transition

**Files:** none.

- [ ] **Step 1:** `uv run pytest -q` → green, no regressions.
- [ ] **Step 2:** Confirm acceptance criteria against `specs/2026-06-23-message-mailbox.md` (ac1 store → T1; ac2 serve → T2; ac3/ac4 inbox/reply/messages → T3; ac5 awaiting derivation → T1/T2/T3; ac6 tests → all). Note any gap.
- [ ] **Step 3:** `mship journal "message mailbox substrate implemented (store + serve + CLI); full suite green" --action completed --test-state pass` then `mship phase review`.

> Then `mship finish --body-file <path>` to open the PR.
<!-- /mship:task -->

---

## Non-goals (from the spec)

Phone chat UI · capture→spec drafting · task-steering · auto-dispatch · notifications · SSE/real-time · edit/delete · attachments · multi-user identity. All deferred to later specs.
