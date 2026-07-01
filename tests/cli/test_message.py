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


def test_reply_needs_you_marks_kind(_configured):
    s = _seed(_configured)
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    t = s.create_thread(subject="x", text="q", now=now)
    r = runner.invoke(app, ["reply", t.id, "look at this", "--needs-you"])
    assert r.exit_code == 0, r.output
    got = s.get(t.id)
    assert got.messages[-1].kind == "needs_you"
    assert got.needs_you is True


def test_reply_defaults_to_note(_configured):
    s = _seed(_configured)
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    t = s.create_thread(subject="x", text="q", now=now)
    r = runner.invoke(app, ["reply", t.id, "just an fyi"])
    assert r.exit_code == 0, r.output
    got = s.get(t.id)
    assert got.messages[-1].kind == "note"
    assert got.needs_you is False


def test_ask_emits_decision(_configured):
    s = _seed(_configured)
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    t = s.create_thread(subject="x", text="q", now=now)
    r = runner.invoke(app, ["ask", t.id, "How to store?",
                            "--option", "File-per-thread", "--option", "SQLite",
                            "--recommend", "0"])
    assert r.exit_code == 0, r.output
    got = s.get(t.id)
    last = got.messages[-1]
    assert last.role == "agent"
    assert last.kind == "decision"
    assert last.decision.options == ["File-per-thread", "SQLite"]
    assert last.decision.recommended == 0
    assert last.decision.allow_free_text is True
    assert got.needs_decision is True


def test_ask_requires_at_least_two_options(_configured):
    s = _seed(_configured)
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    t = s.create_thread(subject="x", text="q", now=now)
    r = runner.invoke(app, ["ask", t.id, "How to store?", "--option", "only-one"])
    assert r.exit_code != 0
    assert s.get(t.id).messages[-1].kind != "decision"


def test_ask_recommend_out_of_range_errors(_configured):
    s = _seed(_configured)
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    t = s.create_thread(subject="x", text="q", now=now)
    r = runner.invoke(app, ["ask", t.id, "How to store?",
                            "--option", "a", "--option", "b", "--recommend", "5"])
    assert r.exit_code != 0
    assert s.get(t.id).messages[-1].kind != "decision"


def test_ask_no_free_text(_configured):
    s = _seed(_configured)
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    t = s.create_thread(subject="x", text="q", now=now)
    r = runner.invoke(app, ["ask", t.id, "How to store?",
                            "--option", "a", "--option", "b", "--no-free-text"])
    assert r.exit_code == 0, r.output
    got = s.get(t.id)
    assert got.messages[-1].decision.allow_free_text is False


def test_ask_unknown_thread_errors(_configured):
    r = runner.invoke(app, ["ask", "nope", "q", "--option", "a", "--option", "b"])
    assert r.exit_code != 0
