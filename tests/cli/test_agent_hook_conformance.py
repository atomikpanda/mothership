from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest
from typer.testing import CliRunner, Result

from mship.cli import app, container
from mship.core.message_store import MessageStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem_store import WorkItemStore


runner = CliRunner()


@dataclass(frozen=True)
class Outcome:
    kind: str
    message: str = ""


@dataclass(frozen=True)
class HarnessWorkspace:
    root: Path
    main: Path
    worktree: Path
    state_dir: Path
    state_manager: StateManager


def _normalize_exit_adapter(result: Result, event_name: str) -> Outcome:
    warning = result.stderr.strip()
    if "adapter failed open" in warning:
        return Outcome("error", warning)
    if event_name == "session_start":
        assert result.exit_code == 0
        return Outcome("context", result.stdout.strip())
    if event_name == "tool_call":
        if result.exit_code == 2:
            return Outcome("deny", warning)
        assert result.exit_code == 0
        return Outcome("allow")

    assert result.exit_code == 0
    if not result.stdout.strip():
        return Outcome("stop")
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    return Outcome("continue", payload["reason"])


def _invoke_exit_adapter(runtime: str, event_name: str, event: dict, env: dict[str, str] | None) -> Outcome:
    command = {
        "session_start": "_session-context",
        "tool_call": "_guard-edit",
        "session_stop": "_drain",
    }[event_name]
    args = [command]
    if runtime != "claude":
        args += ["--runtime", runtime]
    result = runner.invoke(app, args, input=json.dumps(event), env=env)
    return _normalize_exit_adapter(result, event_name)


def invoke_claude_hidden_commands(
    event_name: str,
    event: dict,
    env: dict[str, str] | None = None,
) -> Outcome:
    return _invoke_exit_adapter("claude", event_name, event, env)


def invoke_codex_hidden_commands(
    event_name: str,
    event: dict,
    env: dict[str, str] | None = None,
) -> Outcome:
    return _invoke_exit_adapter("codex", event_name, event, env)


def invoke_omp_hidden_command(
    event_name: str,
    event: dict,
    env: dict[str, str] | None = None,
) -> Outcome:
    result = runner.invoke(
        app,
        ["_omp-hook", event_name],
        input=json.dumps(event),
        env=env,
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    if payload["kind"] == "error":
        return Outcome("error", result.stderr.strip())
    return Outcome(payload["kind"], payload.get("message", ""))


ADAPTERS: dict[str, Callable[[str, dict, dict[str, str] | None], Outcome]] = {
    "claude": invoke_claude_hidden_commands,
    "codex": invoke_codex_hidden_commands,
    "omp": invoke_omp_hidden_command,
}


def _edit_event(runtime: str, *targets: Path, ignored_path: Path | None = None) -> dict:
    if runtime == "omp":
        event = {
            "type": "tool_call",
            "toolName": "edit",
            "input": {"paths": [str(target) for target in targets]},
        }
    elif runtime == "codex":
        headers = ("Add", "Update", "Delete")
        event = {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "\n".join(
                    f"*** {headers[min(index, len(headers) - 1)]} File: {target}"
                    for index, target in enumerate(targets)
                ),
            },
        }
    else:
        event = {
            "hook_event_name": "PreToolUse",
            "tool_name": "MultiEdit" if len(targets) > 1 else "Edit",
            "tool_input": {
                "edits": [{"file_path": str(target)} for target in targets],
            },
        }
    if ignored_path is not None:
        event["ignored_path"] = str(ignored_path)
    return event


def _stop_event(runtime: str, continuation_active: bool) -> dict:
    event = {"stop_hook_active": continuation_active}
    if runtime == "omp":
        event["type"] = "session_stop"
    else:
        event["hook_event_name"] = "Stop"
    return event


@pytest.fixture
def harness_workspace(tmp_path: Path, monkeypatch) -> HarnessWorkspace:
    main = tmp_path / "main"
    (main / "src").mkdir(parents=True)
    (main / "Taskfile.yml").write_text("version: '3'\n")
    worktree = tmp_path / ".worktrees" / "t" / "repo"
    (worktree / "src").mkdir(parents=True)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    config_path = tmp_path / "mothership.yaml"
    config_path.write_text(
        f"workspace: t\nrepos:\n  repo:\n    path: {main}\n    type: library\n"
    )
    work_item = WorkItemStore(state_dir / "workitems").create(
        title="thing",
        kind="bug",
        workspace="t",
        now=datetime.now(timezone.utc),
    )
    task = Task(
        slug="t",
        description="d",
        phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["repo"],
        worktrees={"repo": worktree},
        branch="feat/t",
        work_item_id=work_item.id,
    )
    state_manager = StateManager(state_dir)
    state_manager.save(WorkspaceState(tasks={"t": task}))

    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(config_path)
    container.state_dir.override(state_dir)
    monkeypatch.chdir(tmp_path)
    try:
        yield HarnessWorkspace(tmp_path, main, worktree, state_dir, state_manager)
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()
        container.state_manager.reset_override()
        container.state_manager.reset()
        container.log_manager.reset()


def _outcomes(event_name: str, events: dict[str, dict], env: dict[str, str] | None = None) -> dict[str, Outcome]:
    return {
        runtime: invoke(event_name, events[runtime], env)
        for runtime, invoke in ADAPTERS.items()
    }


def test_session_start_without_active_task_has_equivalent_actionable_context(
    harness_workspace: HarnessWorkspace,
):
    harness_workspace.state_manager.save(WorkspaceState())
    events = {
        "claude": {"hook_event_name": "SessionStart", "source": "startup"},
        "codex": {"hook_event_name": "SessionStart", "source": "startup"},
        "omp": {"type": "session_start"},
    }

    outcomes = _outcomes("session_start", events)

    assert len(set(outcomes.values())) == 1
    outcome = outcomes["claude"]
    assert outcome.kind == "context"
    assert "mship spawn" in outcome.message


def test_valid_worktree_edit_allows_for_every_runtime(harness_workspace: HarnessWorkspace):
    target = harness_workspace.worktree / "src" / "a.py"

    outcomes = _outcomes(
        "tool_call",
        {runtime: _edit_event(runtime, target) for runtime in ADAPTERS},
    )

    assert set(outcomes.values()) == {Outcome("allow")}


def test_adapters_ignore_undocumented_event_fields(harness_workspace: HarnessWorkspace):
    target = harness_workspace.worktree / "src" / "a.py"
    ignored = harness_workspace.main / "src" / "must-not-be-read.py"

    outcomes = _outcomes(
        "tool_call",
        {
            runtime: _edit_event(runtime, target, ignored_path=ignored)
            for runtime in ADAPTERS
        },
    )

    assert set(outcomes.values()) == {Outcome("allow")}


def test_codex_multi_target_fixture_uses_native_apply_patch_headers(tmp_path: Path):
    targets = tuple(tmp_path / f"{name}.py" for name in ("added", "updated", "deleted"))

    event = _edit_event("codex", *targets)

    assert event["tool_name"] == "apply_patch"
    patch = event["tool_input"]["command"]
    assert f"*** Add File: {targets[0]}" in patch
    assert f"*** Update File: {targets[1]}" in patch
    assert f"*** Delete File: {targets[2]}" in patch


def test_any_denied_target_denies_the_whole_native_multi_target_edit(
    harness_workspace: HarnessWorkspace,
):
    added = harness_workspace.worktree / "src" / "added.py"
    denied = harness_workspace.main / "src" / "updated.py"
    deleted = harness_workspace.worktree / "src" / "deleted.py"

    outcomes = _outcomes(
        "tool_call",
        {runtime: _edit_event(runtime, added, denied, deleted) for runtime in ADAPTERS},
    )

    assert len(set(outcomes.values())) == 1
    outcome = outcomes["claude"]
    assert outcome.kind == "deny"
    assert str(harness_workspace.worktree / "src" / denied.name) in outcome.message
    assert "MAIN checkout" in outcome.message


def test_allow_main_edit_environment_override_is_equivalent(harness_workspace: HarnessWorkspace):
    target = harness_workspace.main / "src" / "a.py"

    outcomes = _outcomes(
        "tool_call",
        {runtime: _edit_event(runtime, target) for runtime in ADAPTERS},
        {"MSHIP_ALLOW_MAIN_EDIT": "1"},
    )

    assert set(outcomes.values()) == {Outcome("allow")}


def test_workitem_bypass_preserves_main_checkout_protection(harness_workspace: HarnessWorkspace):
    state = harness_workspace.state_manager.load()
    state.tasks["t"].work_item_id = None
    harness_workspace.state_manager.save(state)
    worktree_target = harness_workspace.worktree / "src" / "a.py"
    main_target = harness_workspace.main / "src" / "a.py"
    env = {"MSHIP_BYPASS_GATE": "1"}

    worktree_outcomes = _outcomes(
        "tool_call",
        {runtime: _edit_event(runtime, worktree_target) for runtime in ADAPTERS},
        env,
    )
    main_outcomes = _outcomes(
        "tool_call",
        {runtime: _edit_event(runtime, main_target) for runtime in ADAPTERS},
        env,
    )

    assert set(worktree_outcomes.values()) == {Outcome("allow")}
    assert len(set(main_outcomes.values())) == 1
    assert main_outcomes["claude"].kind == "deny"
    assert "MAIN checkout" in main_outcomes["claude"].message


@pytest.mark.parametrize("pending_kind", ["reply", "event"])
def test_pending_inbox_work_continues_with_equivalent_actionable_instructions(
    harness_workspace: HarnessWorkspace,
    pending_kind: str,
):
    store = MessageStore(harness_workspace.state_dir / "messages")
    now = datetime.now(timezone.utc)
    thread = store.create_thread("question", "please answer", now)
    if pending_kind == "event":
        store.append(thread.id, "agent", "answered opener", now)
        store.append(thread.id, "agent", "dispatch handoff: act now", now, kind="event")

    outcomes = _outcomes(
        "session_stop",
        {runtime: _stop_event(runtime, False) for runtime in ADAPTERS},
    )

    assert len(set(outcomes.values())) == 1
    outcome = outcomes["claude"]
    assert outcome.kind == "continue"
    if pending_kind == "reply":
        assert "mship reply" in outcome.message
    else:
        assert "FOR THE AGENT" in outcome.message
        assert "act" in outcome.message


def test_continuation_active_stop_is_bounded_for_every_runtime(
    harness_workspace: HarnessWorkspace,
):
    MessageStore(harness_workspace.state_dir / "messages").create_thread(
        "question", "still pending", datetime.now(timezone.utc)
    )

    outcomes = _outcomes(
        "session_stop",
        {runtime: _stop_event(runtime, True) for runtime in ADAPTERS},
    )

    assert set(outcomes.values()) == {Outcome("stop")}


def test_guard_adapter_exceptions_fail_open_and_warn_without_event_data(
    harness_workspace: HarnessWorkspace,
):
    secret = "event-body-must-not-leak"
    events = {
        "claude": {"tool_name": "Edit", "tool_input": {"secret": secret}},
        "codex": {"tool_name": "Edit", "tool_input": {"secret": secret}},
        "omp": {"toolName": "edit", "input": {"secret": secret}},
    }

    outcomes = _outcomes("tool_call", events)

    assert {outcome.kind for outcome in outcomes.values()} == {"error"}
    for runtime, outcome in outcomes.items():
        warning = outcome.message.lower()
        assert runtime in warning
        assert ("pretooluse" if runtime != "omp" else "tool_call") in warning
        assert "failed open" in warning
        assert secret not in outcome.message


@pytest.mark.parametrize(
    ("event_name", "patched_owner", "lifecycle_names"),
    [
        ("session_start", "session_start", {"claude": "sessionstart", "codex": "sessionstart", "omp": "session_start"}),
        ("session_stop", "MessageStore", {"claude": "stop", "codex": "stop", "omp": "session_stop"}),
    ],
)
def test_other_lifecycle_adapter_exceptions_fail_open_and_warn(
    harness_workspace: HarnessWorkspace,
    monkeypatch,
    event_name: str,
    patched_owner: str,
    lifecycle_names: dict[str, str],
):
    if patched_owner == "session_start":
        from mship.core import agent_hooks

        monkeypatch.setattr(agent_hooks, "session_start", lambda *_args: (_ for _ in ()).throw(RuntimeError()))
    else:
        from mship.core import message_store

        monkeypatch.setattr(message_store, "MessageStore", lambda *_args: (_ for _ in ()).throw(RuntimeError()))

    events = {
        runtime: (_stop_event(runtime, False) if event_name == "session_stop" else {})
        for runtime in ADAPTERS
    }
    outcomes = _outcomes(event_name, events)

    assert {outcome.kind for outcome in outcomes.values()} == {"error"}
    for runtime, outcome in outcomes.items():
        warning = outcome.message.lower()
        assert runtime in warning
        assert lifecycle_names[runtime] in warning
        assert "failed open" in warning
