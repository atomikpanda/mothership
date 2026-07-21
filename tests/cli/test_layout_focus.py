from mship.cli.layout import decide_focus_action, tab_name_for


def test_tab_name_is_deterministic_and_id_based():
    assert tab_name_for("wi-20260721-abc") == tab_name_for("wi-20260721-abc")
    assert "wi-20260721-abc" in tab_name_for("wi-20260721-abc")


def test_decision_create_when_absent():
    assert decide_focus_action("wi-1", [], is_done=False) == "create"


def test_decision_go_to_when_present():
    assert decide_focus_action("wi-1", ["other", "wi-1"], is_done=False) == "go-to"


def test_decision_close_when_done_and_present():
    assert decide_focus_action("wi-1", ["wi-1"], is_done=True) == "close"


def test_decision_noop_when_done_and_absent():
    assert decide_focus_action("wi-1", ["other"], is_done=True) == "noop"


# --- Task 4: per-WorkItem KDL renderer ---------------------------------------
from mship.cli.layout import (
    default_phase_tab, render_workitem_layout, resolve_chat_command,
)


def test_resolve_chat_command_precedence():
    assert resolve_chat_command("claude", {}) == "claude"
    assert resolve_chat_command(None, {"MSHIP_CHAT_COMMAND": "my-agent"}) == "my-agent"
    assert resolve_chat_command(None, {}) is None  # default: bare shell pane


def test_default_phase_tab_mapping():
    assert default_phase_tab("shaping") == "Plan"
    assert default_phase_tab("ready") == "Plan"
    assert default_phase_tab("in_flight") == "Dev"
    assert default_phase_tab("review") == "Review"
    assert default_phase_tab("done") == "Run"
    assert default_phase_tab("something-else") == "Plan"


def _kdl(**over):
    base = dict(name="wi-1", worktree="/wt/a", item_id="wi-1", task_slug="a",
                chat_command=None, default_phase="Dev")
    base.update(over)
    return render_workitem_layout(**base)


def test_kdl_is_chat_first_with_editor_and_cwd():
    kdl = _kdl()
    assert 'tab name="wi-1" focus=true' in kdl
    assert 'cwd "/wt/a"' in kdl
    assert 'name="Agent"' in kdl
    assert 'name="Editor"' in kdl
    # Default chat command == bare shell pane (no command= on Agent).
    assert 'name="Agent" focus=true {' not in kdl  # bare pane has no child block


def test_kdl_configurable_chat_command():
    kdl = _kdl(chat_command="claude")
    assert 'name="Agent"' in kdl and 'command="sh"' in kdl
    assert '"-c" "claude"' in kdl


def test_kdl_has_all_four_phase_subtabs():
    kdl = _kdl()
    for phase in ("Plan", "Dev", "Review", "Run"):
        assert f'swap_tiled_layout name="{phase}"' in kdl


def test_kdl_bakes_shipped_view_commands_with_item_and_task():
    kdl = _kdl()
    assert '"view" "spec" "--workitem" "wi-1" "--watch"' in kdl   # Plan
    assert '"view" "diff" "--task" "a" "--watch"' in kdl           # Dev/Review
    assert '"view" "journal" "--task" "a" "--watch"' in kdl        # Dev + Run
    assert '"view" "item" "wi-1"' in kdl                            # Review (PR/checks)
    # NOTE: `mship view logs` does not exist — the only run/journal view command is
    # `view journal` (logs.py registers it as `journal`). The Run sub-tab therefore
    # tails `view journal` (asserted above), not a nonexistent `view logs`.


def test_kdl_escapes_worktree_path():
    kdl = _kdl(worktree='/wt/ba"d')
    assert 'cwd "/wt/ba\\"d"' in kdl


def test_kdl_without_task_degrades_task_scoped_panes():
    kdl = _kdl(task_slug=None)
    assert "--task" not in kdl
    assert 'name="Shell"' in kdl   # task-scoped panes fall back to a shell


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


def _patch_zellij(monkeypatch, *, in_session, existing, action_ok=True, query_ok=True):
    calls = []

    def _run(args):
        calls.append(args)
        return action_ok

    monkeypatch.setattr(layout_mod, "_in_zellij", lambda: in_session)
    monkeypatch.setattr(layout_mod, "_query_tab_names",
                        lambda: (list(existing) if query_ok else None))
    monkeypatch.setattr(layout_mod, "_run_zellij_action", _run)
    return calls


def test_write_workitem_layout_file_writes_stable_named_kdl(tmp_path, monkeypatch):
    # The pure helper: writes <cache>/<item_id>.kdl and returns (name, dir) that
    # `new-tab --layout <name> --layout-dir <dir>` resolves by name.
    from mship.cli.layout import write_workitem_layout_file

    monkeypatch.setenv("HOME", str(tmp_path))
    name, cache_dir = write_workitem_layout_file("wi-1", "layout { }\n")
    assert name == "wi-1"
    assert (cache_dir / "wi-1.kdl").read_text() == "layout { }\n"
    # Re-writing overwrites the same stable path (no accumulation, no delete-race).
    name2, cache_dir2 = write_workitem_layout_file("wi-1", "layout { changed }\n")
    assert cache_dir2 == cache_dir
    assert (cache_dir / "wi-1.kdl").read_text() == "layout { changed }\n"


# --- Task 8: explicit close + close-on-done lifecycle ------------------------
def test_close_closes_existing_tab(tmp_path, monkeypatch):
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    calls = _patch_zellij(monkeypatch, in_session=True, existing=["wi-1"])
    try:
        result = runner.invoke(app, ["layout", "close", "wi-1"])
        assert result.exit_code == 0, result.output
        assert calls == [["go-to-tab-name", "wi-1"], ["close-tab"]]
    finally:
        _reset_focus()


def test_close_no_tab_is_noop(tmp_path, monkeypatch):
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    calls = _patch_zellij(monkeypatch, in_session=True, existing=["Overview"])
    try:
        result = runner.invoke(app, ["layout", "close", "wi-1"])
        assert result.exit_code == 0, result.output
        assert calls == []
    finally:
        _reset_focus()


def test_close_outside_zellij_noops(tmp_path, monkeypatch):
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    calls = _patch_zellij(monkeypatch, in_session=False, existing=["wi-1"])
    try:
        result = runner.invoke(app, ["layout", "close", "wi-1"])
        assert result.exit_code == 0
        assert "zellij" in result.output.lower()
        assert calls == []
    finally:
        _reset_focus()


# --- Greptile #396: seams report failure; never close the wrong tab ---
def test_close_does_not_close_wrong_tab_when_go_to_fails(tmp_path, monkeypatch):
    # go-to fails -> close-tab must NOT run (else it closes the active/wrong tab).
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    calls = _patch_zellij(monkeypatch, in_session=True, existing=["wi-1"], action_ok=False)
    try:
        result = runner.invoke(app, ["layout", "close", "wi-1"])
        assert result.exit_code == 1
        assert calls == [["go-to-tab-name", "wi-1"]]   # close-tab NOT called
    finally:
        _reset_focus()


def test_close_errors_when_tab_query_fails(tmp_path, monkeypatch):
    _seed_focus(tmp_path, {"r": tmp_path / "wt-a"})
    calls = _patch_zellij(monkeypatch, in_session=True, existing=["wi-1"], query_ok=False)
    try:
        result = runner.invoke(app, ["layout", "close", "wi-1"])
        assert result.exit_code == 1
        assert calls == []
    finally:
        _reset_focus()


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
