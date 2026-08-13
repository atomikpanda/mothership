from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mship.core import agent_hooks
from mship.core.agent_hooks import DecisionKind, Runtime
from mship.core.edit_guard import GuardDecision


def test_session_start_combines_shared_context(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(agent_hooks, "no_task_notice", lambda cwd: "no task")
    monkeypatch.setattr(agent_hooks, "messaging_notice", lambda cwd: "arm inbox")

    decision = agent_hooks.session_start(Runtime.CODEX, tmp_path)

    assert decision.kind is DecisionKind.CONTEXT
    assert decision.message == "no task\narm inbox"
    assert decision.runtime is Runtime.CODEX


def test_session_start_empty_context_is_valid(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(agent_hooks, "no_task_notice", lambda cwd: None)
    monkeypatch.setattr(agent_hooks, "messaging_notice", lambda cwd: None)

    decision = agent_hooks.session_start(Runtime.OMP, tmp_path)

    assert decision.kind is DecisionKind.CONTEXT
    assert decision.message == ""


def test_pre_tool_use_evaluates_every_deduplicated_target(monkeypatch, tmp_path: Path):
    calls: list[Path] = []

    def evaluate(target, state, config, **kwargs):
        calls.append(Path(target))
        allowed = Path(target).name != "blocked.py"
        return GuardDecision(allowed=allowed, reason="blocked target" if not allowed else "")

    monkeypatch.setattr(agent_hooks, "evaluate_edit", evaluate)

    decision = agent_hooks.pre_tool_use(
        runtime=Runtime.CODEX,
        cwd=tmp_path,
        targets=("src/ok.py", "src/ok.py", "src/blocked.py"),
        state=object(),
        config=object(),
        workspace_root=tmp_path,
        bypass_main_edit=False,
        bypass_workitem_gate=False,
    )

    assert calls == [tmp_path / "src/ok.py", tmp_path / "src/blocked.py"]
    assert decision.kind is DecisionKind.DENY
    assert decision.message == "blocked target"


def test_pre_tool_use_main_edit_bypass_skips_policy(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        agent_hooks,
        "evaluate_edit",
        lambda *args, **kwargs: pytest.fail("policy should not run"),
    )

    decision = agent_hooks.pre_tool_use(
        runtime=Runtime.CLAUDE,
        cwd=tmp_path,
        targets=("src/x.py",),
        state=object(),
        config=object(),
        workspace_root=tmp_path,
        bypass_main_edit=True,
        bypass_workitem_gate=False,
    )

    assert decision.kind is DecisionKind.ALLOW


def test_pre_tool_use_requires_a_target(tmp_path: Path):
    with pytest.raises(ValueError, match="target"):
        agent_hooks.pre_tool_use(
            runtime=Runtime.OMP,
            cwd=tmp_path,
            targets=(),
            state=object(),
            config=object(),
            workspace_root=tmp_path,
            bypass_main_edit=False,
            bypass_workitem_gate=False,
        )


def test_stop_continues_with_pending_threads(monkeypatch):
    thread = SimpleNamespace(
        id="thread-1",
        subject="Question",
        awaiting_reply=True,
        awaiting_agent_event=False,
        messages=[SimpleNamespace(text="Please answer", role="human", kind="note")],
    )
    store = SimpleNamespace(list=lambda: [thread])
    stamped: list[list] = []
    monkeypatch.setattr(
        agent_hooks,
        "stamp_agent_seen",
        lambda actual_store, threads, now: stamped.append(threads),
    )

    decision = agent_hooks.stop(
        runtime=Runtime.OMP,
        store=store,
        continuation_active=False,
        now=datetime.now(timezone.utc),
    )

    assert decision.kind is DecisionKind.CONTINUE
    assert "thread-1" in decision.message
    assert stamped == [[thread]]


def test_stop_reentry_is_bounded_without_reading_store():
    store = SimpleNamespace(list=lambda: pytest.fail("store should not be read on re-entry"))

    decision = agent_hooks.stop(
        runtime=Runtime.CODEX,
        store=store,
        continuation_active=True,
    )

    assert decision.kind is DecisionKind.STOP


def test_stop_empty_inbox_allows_stop(monkeypatch):
    store = SimpleNamespace(list=lambda: [])
    monkeypatch.setattr(agent_hooks, "stamp_agent_seen", lambda *args: None)

    decision = agent_hooks.stop(
        runtime=Runtime.CLAUDE,
        store=store,
        continuation_active=False,
    )

    assert decision.kind is DecisionKind.STOP


def test_stop_store_failure_is_not_converted_to_policy_stop():
    def fail():
        raise OSError("store unavailable")

    with pytest.raises(OSError, match="store unavailable"):
        agent_hooks.stop(
            runtime=Runtime.OMP,
            store=SimpleNamespace(list=fail),
            continuation_active=False,
        )
