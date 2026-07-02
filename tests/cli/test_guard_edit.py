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
    """Workspace with one active task (linked to a bug WorkItem, so it clears
    the WorkItem gate without needing an approved spec); returns
    (cfg, state_dir, main_repo)."""
    from mship.core.workitem_store import WorkItemStore

    main = tmp_path / "main"; (main / "src").mkdir(parents=True)
    (main / "Taskfile.yml").write_text("version: '3'\n")
    wt = tmp_path / ".worktrees" / "t" / "repo"; (wt / "src").mkdir(parents=True)
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(f"workspace: t\nrepos:\n  repo:\n    path: {main}\n    type: library\n")
    wi = WorkItemStore(state_dir / "workitems").create(
        title="thing", kind="bug", workspace="t", now=datetime.now(timezone.utc),
    )
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["repo"], worktrees={"repo": wt}, branch="feat/t",
        work_item_id=wi.id,
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
        # The deny reason is written to stderr; assert on that stream directly
        # rather than result.output (Click 8.2+ no longer folds stderr into it).
        assert "MAIN checkout" in result.stderr
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


# ---------------------------------------------------------------------------
# WorkItem gate (task 5/6): an edit inside the task's OWN worktree is still
# blocked if the task fails core/workitem_gate.py::check_task_gate (no
# WorkItem, or a feature WorkItem without an approved spec). MSHIP_BYPASS_GATE
# is the hotfix escape — distinct from MSHIP_ALLOW_MAIN_EDIT above.
# ---------------------------------------------------------------------------

def _bootstrap_no_workitem(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Like _bootstrap, but the task has no WorkItem at all."""
    main = tmp_path / "main"; (main / "src").mkdir(parents=True)
    (main / "Taskfile.yml").write_text("version: '3'\n")
    wt = tmp_path / ".worktrees" / "t" / "repo"; (wt / "src").mkdir(parents=True)
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(f"workspace: t\nrepos:\n  repo:\n    path: {main}\n    type: library\n")
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["repo"], worktrees={"repo": wt}, branch="feat/t",
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"t": task}))
    return cfg, state_dir, main


def test_blocks_worktree_edit_when_task_has_no_workitem(tmp_path: Path):
    cfg, state_dir, _ = _bootstrap_no_workitem(tmp_path)
    wt_file = tmp_path / ".worktrees" / "t" / "repo" / "src" / "x.py"
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_guard-edit"], input=_event(wt_file))
        assert result.exit_code == 2
        assert "WorkItem" in result.stderr
    finally:
        _reset()


def test_bypass_gate_env_allows_worktree_edit_without_workitem(tmp_path: Path, monkeypatch):
    cfg, state_dir, _ = _bootstrap_no_workitem(tmp_path)
    wt_file = tmp_path / ".worktrees" / "t" / "repo" / "src" / "x.py"
    monkeypatch.setenv("MSHIP_BYPASS_GATE", "1")
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_guard-edit"], input=_event(wt_file))
        assert result.exit_code == 0
    finally:
        _reset()


def test_bypass_gate_does_not_lift_main_checkout_block(tmp_path: Path, monkeypatch):
    """MSHIP_BYPASS_GATE is the WorkItem-gate escape only — it must not also
    reopen the (separately-gated) main-checkout block."""
    cfg, state_dir, main = _bootstrap_no_workitem(tmp_path)
    monkeypatch.setenv("MSHIP_BYPASS_GATE", "1")
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["_guard-edit"], input=_event(main / "src" / "x.py"))
        assert result.exit_code == 2
        assert "MAIN checkout" in result.stderr
    finally:
        _reset()
