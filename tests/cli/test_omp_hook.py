from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.message_store import MessageStore
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


def _bootstrap(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    from mship.core.workitem_store import WorkItemStore

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
    StateManager(state_dir).save(WorkspaceState(tasks={"t": task}))
    return config_path, state_dir, main, worktree


def _override(config_path: Path, state_dir: Path) -> None:
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(config_path)
    container.state_dir.override(state_dir)


def _reset() -> None:
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.log_manager.reset()


def test_session_start_returns_private_context_envelope(tmp_path: Path, monkeypatch):
    from mship.core import agent_hooks
    from mship.core.agent_hooks import AgentHookDecision, DecisionKind, Runtime

    monkeypatch.setattr(
        agent_hooks,
        "session_start",
        lambda runtime, cwd: AgentHookDecision(Runtime.OMP, DecisionKind.CONTEXT, "context"),
    )

    result = runner.invoke(app, ["_omp-hook", "session_start"], input="{}")

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"kind": "context", "message": "context"}


def test_tool_call_returns_deny_envelope(tmp_path: Path):
    config_path, state_dir, main, _ = _bootstrap(tmp_path)
    _override(config_path, state_dir)
    try:
        result = runner.invoke(
            app,
            ["_omp-hook", "tool_call"],
            input=json.dumps({
                "type": "tool_call",
                "toolName": "write",
                "input": {"path": str(main / "src" / "x.py")},
            }),
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["kind"] == "deny"
        assert "MAIN checkout" in payload["message"]
    finally:
        _reset()


def test_tool_call_adapter_error_fails_open_with_error_envelope(tmp_path: Path):
    config_path, state_dir, _, _ = _bootstrap(tmp_path)
    _override(config_path, state_dir)
    try:
        result = runner.invoke(
            app,
            ["_omp-hook", "tool_call"],
            input=json.dumps({"type": "tool_call", "toolName": "edit", "input": {}}),
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"kind": "error"}
        assert "failed open" in result.stderr
    finally:
        _reset()


def test_session_stop_returns_continue_then_bounded_stop(tmp_path: Path):
    config_path, state_dir, _, _ = _bootstrap(tmp_path)
    store = MessageStore(state_dir / "messages")
    store.create_thread("question", "please answer", datetime.now(timezone.utc))
    _override(config_path, state_dir)
    try:
        first = runner.invoke(
            app,
            ["_omp-hook", "session_stop"],
            input=json.dumps({"type": "session_stop", "stop_hook_active": False}),
        )
        second = runner.invoke(
            app,
            ["_omp-hook", "session_stop"],
            input=json.dumps({"type": "session_stop", "stop_hook_active": True}),
        )
        assert json.loads(first.stdout)["kind"] == "continue"
        assert "please answer" in json.loads(first.stdout)["message"]
        assert json.loads(second.stdout) == {"kind": "stop"}
    finally:
        _reset()
