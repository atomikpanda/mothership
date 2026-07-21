from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.cli.layout import (
    _BASE_TABS,
    _LAYOUT_HEAD,
    _LAYOUT_TAIL,
    _TEMPLATE,
    render_serve_layout,
    serve_cli_args,
)

runner = CliRunner()


def _expected_path(tmp_path: Path) -> Path:
    return tmp_path / ".config" / "zellij" / "layouts" / "mothership.kdl"


def _serve_path(tmp_path: Path) -> Path:
    return tmp_path / ".config" / "zellij" / "layouts" / "mothership-serve.kdl"


# --- Task 1: template refactor -------------------------------------------------

def test_template_reconstructs_from_parts():
    assert _TEMPLATE == _LAYOUT_HEAD + _BASE_TABS + _LAYOUT_TAIL


def test_base_layout_has_no_phase_tabs():
    # The legacy Plan/Dev/Review/Run base tabs were removed: the base is Overview
    # (launchpad) + the focus-driven Cockpit tab.
    assert _BASE_TABS == ""
    for name in ("Plan", "Dev", "Review", "Run"):
        assert f'tab name="{name}"' not in _TEMPLATE
    assert 'tab name="Overview"' in _TEMPLATE
    assert 'tab name="Cockpit" focus=true' in _TEMPLATE


# --- Task 2: pure builders -----------------------------------------------------

def test_serve_cli_args_mapping():
    assert serve_cli_args(host=None, port=None, relay=False, relay_host=None) == []
    assert serve_cli_args(host="0.0.0.0", port=8080, relay=False, relay_host=None) == [
        "--host", "0.0.0.0", "--port", "8080",
    ]
    assert serve_cli_args(host=None, port=None, relay=True, relay_host=None) == ["--relay"]
    assert serve_cli_args(host=None, port=None, relay=False, relay_host="r.example.com") == [
        "--relay-host", "r.example.com",
    ]
    assert serve_cli_args(host="1.2.3.4", port=47100, relay=True, relay_host=None) == [
        "--host", "1.2.3.4", "--port", "47100", "--relay",
    ]


def test_render_serve_layout_no_args_is_overview_plus_serve():
    kdl = render_serve_layout([])
    # Base is Overview (launchpad) + Cockpit; serve appends a Serve tab. No phase tabs.
    assert 'tab name="Overview"' in kdl
    assert 'tab name="Cockpit"' in kdl
    assert 'tab name="Serve"' in kdl
    for name in ("Plan", "Dev", "Review", "Run"):
        assert f'tab name="{name}"' not in kdl
    assert 'command="mship"' in kdl
    assert 'args "serve";' in kdl
    # Serve tab comes AFTER Overview.
    assert kdl.index('tab name="Overview"') < kdl.index('tab name="Serve"')


def test_template_has_overview_launchpad_tab():
    assert 'tab name="Overview"' in _TEMPLATE
    assert '"view" "queue"' in _TEMPLATE
    assert '"view" "items"' in _TEMPLATE


def test_render_serve_layout_threads_flags():
    kdl = render_serve_layout(["--relay", "--port", "8080"])
    assert 'args "serve" "--relay" "--port" "8080";' in kdl


def test_render_serve_layout_escapes_kdl_strings():
    # A value containing a double-quote must be escaped so the KDL stays valid
    # (Greptile #364: unescaped user strings could break out of the string).
    kdl = render_serve_layout(["--host", 'ba"d'])
    assert '"--host" "ba\\"d";' in kdl


# --- Task 3: init writes both layouts -----------------------------------------

def test_layout_init_writes_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["layout", "init"])
    assert result.exit_code == 0, result.output
    assert _expected_path(tmp_path).read_text() == _TEMPLATE


def test_launch_layout_path_is_process_keyed(tmp_path: Path, monkeypatch):
    # Per-PID path so two concurrent `mship layout launch` processes write DIFFERENT
    # files and can't race on a shared path before zellij reads it.
    import os as _os
    monkeypatch.setenv("HOME", str(tmp_path))
    from mship.cli.layout import _launch_layout_path
    p = _launch_layout_path()
    assert str(_os.getpid()) in p.name
    assert p.suffix == ".kdl"
    assert "mothership-serve-launch.kdl" not in str(p)  # not the old shared path


def test_init_writes_both_layouts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["layout", "init"])
    assert result.exit_code == 0, result.output
    assert _expected_path(tmp_path).read_text() == _TEMPLATE
    serve = _serve_path(tmp_path).read_text()
    assert 'tab name="Serve"' in serve
    assert 'args "serve";' in serve


def test_layout_init_refuses_when_exists_without_force(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = _expected_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    original_content = "original content"
    target.write_text(original_content)

    result = runner.invoke(app, ["layout", "init"])
    assert result.exit_code == 1
    assert target.read_text() == original_content


def test_init_refuses_when_serve_exists_without_force(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    serve = _serve_path(tmp_path)
    serve.parent.mkdir(parents=True, exist_ok=True)
    serve.write_text("keep me")
    result = runner.invoke(app, ["layout", "init"])
    assert result.exit_code == 1
    assert serve.read_text() == "keep me"


def test_layout_init_overwrites_with_force(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = _expected_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old content")

    result = runner.invoke(app, ["layout", "init", "--force"])
    assert result.exit_code == 0, result.output
    assert target.read_text() == _TEMPLATE
    assert 'tab name="Serve"' in _serve_path(tmp_path).read_text()


# --- Task 4: launch selects/renders the serve layout ---------------------------

def _capture_launch(monkeypatch, argv):
    captured = {}

    def fake_execvp(file, args):
        captured["file"] = file
        captured["args"] = args

    monkeypatch.setattr("os.execvp", fake_execvp)
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    return captured


def test_launch_renders_cockpit_temp_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cap = _capture_launch(monkeypatch, ["layout", "launch"])
    assert cap["args"][0:2] == ["zellij", "--layout"]
    body = Path(cap["args"][2]).read_text()
    assert 'tab name="Cockpit" focus=true' in body
    assert 'pane stacked=true {' in body
    assert '"view" "diff" "--follow"' in body


def test_launch_threads_chat_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cap = _capture_launch(monkeypatch, ["layout", "launch", "--chat-command", "claude"])
    body = Path(cap["args"][2]).read_text()
    assert '"-c" "claude"' in body


def test_launch_serve_still_adds_serve_tab(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cap = _capture_launch(monkeypatch, ["layout", "launch", "--serve"])
    body = Path(cap["args"][2]).read_text()
    assert 'tab name="Serve"' in body
    assert 'tab name="Cockpit"' in body


def test_launch_serve_renders_temp_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cap = _capture_launch(monkeypatch, ["layout", "launch", "--serve"])
    assert cap["args"][0:2] == ["zellij", "--layout"]
    layout_path = Path(cap["args"][2])
    assert layout_path != Path("mothership")
    body = layout_path.read_text()
    assert 'tab name="Serve"' in body
    assert 'args "serve";' in body


def test_launch_serve_threads_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cap = _capture_launch(monkeypatch, ["layout", "launch", "--serve", "--relay", "--port", "8080"])
    body = Path(cap["args"][2]).read_text()
    # serve_cli_args emits a stable canonical order (host, port, relay, relay_host);
    # flag order is immaterial to `mship serve`.
    assert 'args "serve" "--port" "8080" "--relay";' in body


def test_launch_serve_flag_implies_serve(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cap = _capture_launch(monkeypatch, ["layout", "launch", "--relay"])
    body = Path(cap["args"][2]).read_text()
    assert 'args "serve" "--relay";' in body


# The former `test_review_tab_has_journal_pane` asserted on the base _TEMPLATE
# Review tab, which no longer exists (base is Overview-only). The per-item Review
# phase sub-tab's journal/diff panes are covered by test_layout_focus.py
# (test_kdl_bakes_shipped_view_commands_with_item_and_task).


# --- cockpit-v2 Task 9: single-cockpit builder (ONE fixed rich stack) ---
from mship.cli.layout import (
    ViewPaneSpec, cockpit_view_specs, render_cockpit_layout,
)


def test_cockpit_view_specs_is_the_fixed_rich_set_in_order():
    # A single fixed member set, same regardless of phase: Spec, Diff, Journal,
    # Status, Item/PR — every member is a `mship view … --follow` command.
    specs = cockpit_view_specs()
    assert [s.name for s in specs] == ["Spec", "Diff", "Journal", "Status", "Item"]
    assert [s.view_args for s in specs] == [
        ["view", "spec", "--follow"],
        ["view", "diff", "--follow"],
        ["view", "journal", "--follow"],
        ["view", "status", "--follow"],
        ["view", "item", "--follow"],
    ]


def test_cockpit_view_specs_takes_no_arguments():
    # No phase parameter — membership never depends on the focused item's phase.
    import inspect
    assert list(inspect.signature(cockpit_view_specs).parameters) == []


def test_cockpit_agent_pane_is_outside_the_stack():
    kdl = render_cockpit_layout()
    assert 'name="Agent" focus=true' in kdl
    # Agent is declared before the stacked group and never inside it.
    assert kdl.index('name="Agent"') < kdl.index('pane stacked=true {')


def test_cockpit_has_stacked_group_of_all_five_follow_views():
    kdl = render_cockpit_layout()
    assert 'pane stacked=true {' in kdl
    assert '"view" "spec" "--follow"' in kdl
    assert '"view" "diff" "--follow"' in kdl
    assert '"view" "journal" "--follow"' in kdl
    assert '"view" "status" "--follow"' in kdl
    assert '"view" "item" "--follow"' in kdl


def test_cockpit_keeps_overview_launchpad():
    kdl = render_cockpit_layout()
    assert 'tab name="Overview"' in kdl
    assert '"view" "queue"' in kdl and '"view" "items"' in kdl
    assert 'tab name="Cockpit" focus=true' in kdl


def test_cockpit_chat_command_threaded():
    kdl = render_cockpit_layout(chat_command="claude")
    assert 'name="Agent" focus=true command="sh"' in kdl
    assert '"-c" "claude"' in kdl


def test_cockpit_membership_is_identical_regardless_of_call():
    # The stack is fixed: two builds produce the same member set (no phase input).
    assert render_cockpit_layout() == render_cockpit_layout()
