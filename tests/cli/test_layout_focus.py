# --- resolve_chat_command (kept: used by cockpit launch) ---------------------
from mship.cli.layout import resolve_chat_command


def test_resolve_chat_command_precedence():
    assert resolve_chat_command("claude", {}) == "claude"
    assert resolve_chat_command(None, {"MSHIP_CHAT_COMMAND": "my-agent"}) == "my-agent"
    assert resolve_chat_command(None, {}) is None  # default: bare shell pane


# --- Task 5: resolve_focus_target --------------------------------------------
from datetime import datetime, timezone
from pathlib import Path

from mship.cli import container
from mship.cli.layout import resolve_focus_target
from mship.core.spec import Spec
from mship.core.spec_store import SPECS_DIRNAME, SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem import WorkItem
from mship.core.workitem_store import WorkItemStore


def _dt():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _seed_focus(tmp_path, worktrees):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    (tmp_path / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")
    SpecStore(tmp_path / SPECS_DIRNAME).save(Spec(
        id="spec-1", title="Overhaul", status="approved",
        created_at=_dt(), updated_at=_dt(), body="b\n"))
    WorkItemStore(state_dir / "workitems").save(WorkItem(
        id="wi-1", title="Overhaul", workspace="t", kind="feature",
        created_at=_dt(), updated_at=_dt(), spec_id="spec-1", task_slugs=["a"]))
    StateManager(state_dir).save(WorkspaceState(tasks={"a": Task(
        slug="a", description="d", phase="dev", created_at=_dt(),
        affected_repos=["r"], branch="feat/a", worktrees=worktrees, work_item_id="wi-1")}))
    container.config.reset(); container.state_manager.reset()
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)


def _reset_focus():
    container.config_path.reset_override(); container.state_dir.reset_override()
    container.config.reset_override(); container.config.reset()
    container.state_manager.reset_override(); container.state_manager.reset()


def test_resolve_focus_target_returns_worktree_and_task(tmp_path):
    wt = tmp_path / "wt-a"
    _seed_focus(tmp_path, {"r": wt})
    try:
        summary, task_slug, worktree = resolve_focus_target(container, "wi-1")
        assert summary.id == "wi-1"
        assert task_slug == "a"
        assert worktree == wt
    finally:
        _reset_focus()


def test_resolve_focus_target_unknown_id_is_none(tmp_path):
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    try:
        assert resolve_focus_target(container, "wi-missing") is None
    finally:
        _reset_focus()


def test_resolve_focus_target_no_worktree_falls_back_to_workspace_root(tmp_path):
    _seed_focus(tmp_path, {})
    try:
        summary, task_slug, worktree = resolve_focus_target(container, "wi-1")
        assert worktree == tmp_path   # workspace root (config_path parent)
    finally:
        _reset_focus()


# --- Task 6: mship layout focus driver ---------------------------------------
import mship.cli.layout as layout_mod
from mship.cli import app
from typer.testing import CliRunner

runner = CliRunner()


def _patch_zellij(monkeypatch, *, in_session, existing=None, action_ok=True, query_ok=True):
    """Cockpit-v2: `layout focus` no longer runs any zellij action, so the only seam
    left to control is `_in_zellij`. Returns an always-empty `calls` list so the
    Task-2 tests can still assert 'no zellij action was attempted'."""
    calls: list = []
    monkeypatch.setattr(layout_mod, "_in_zellij", lambda: in_session)
    return calls


# --- cockpit-v2 Task 2: focus sets the focus file, no tabs ---
from mship.core.focus import focus_path, read_focus


def test_focus_writes_focus_file_no_zellij(tmp_path, monkeypatch):
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    calls = _patch_zellij(monkeypatch, in_session=True, existing=["Overview"])
    try:
        result = runner.invoke(app, ["layout", "focus", "wi-1"])
        assert result.exit_code == 0, result.output
        assert read_focus(focus_path(tmp_path / ".mothership")).work_item_id == "wi-1"
        assert calls == []   # NEVER touches zellij tabs anymore
    finally:
        _reset_focus()


def test_focus_show_prints_current_focus(tmp_path, monkeypatch):
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    try:
        runner.invoke(app, ["layout", "focus", "wi-1"])
        result = runner.invoke(app, ["layout", "focus", "--show"])
        assert result.exit_code == 0, result.output
        assert "wi-1" in result.output
    finally:
        _reset_focus()


def test_focus_show_when_none(tmp_path, monkeypatch):
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    try:
        result = runner.invoke(app, ["layout", "focus", "--show"])
        assert result.exit_code == 0, result.output
        assert "no workitem" in result.output.lower()
    finally:
        _reset_focus()


def test_focus_unknown_id_exits_1_no_write(tmp_path, monkeypatch):
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    try:
        result = runner.invoke(app, ["layout", "focus", "wi-missing"])
        assert result.exit_code == 1
        assert "wi-missing" in result.output
        assert read_focus(focus_path(tmp_path / ".mothership")) is None
    finally:
        _reset_focus()


def test_focus_done_item_is_flagged_but_written(tmp_path, monkeypatch):
    from mship.core.spec_store import SPECS_DIRNAME, SpecStore
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    store = SpecStore(tmp_path / SPECS_DIRNAME)
    store.save(store.find_by_id("spec-1").model_copy(update={"status": "archived"}))
    try:
        result = runner.invoke(app, ["layout", "focus", "wi-1"])
        assert result.exit_code == 0, result.output
        assert "done" in result.output.lower()
        assert read_focus(focus_path(tmp_path / ".mothership")).work_item_id == "wi-1"
    finally:
        _reset_focus()


def test_focus_outside_zellij_still_writes_with_advisory(tmp_path, monkeypatch):
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    monkeypatch.setattr("mship.cli.layout._in_zellij", lambda: False)
    try:
        result = runner.invoke(app, ["layout", "focus", "wi-1"])
        assert result.exit_code == 0, result.output
        assert read_focus(focus_path(tmp_path / ".mothership")).work_item_id == "wi-1"
        assert "zellij" in result.output.lower()   # advisory, not an error
    finally:
        _reset_focus()
