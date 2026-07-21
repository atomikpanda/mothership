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
