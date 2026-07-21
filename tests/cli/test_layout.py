from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.cli.layout import (
    _BASE_TABS,
    _DEFAULT_TAB_TEMPLATE,
    _LAYOUT_HEAD,
    _LAYOUT_TAIL,
    _TEMPLATE,
    render_serve_layout,
    render_workitem_layout,
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
    # only (the phase sub-tabs now live inside each WorkItem's dedicated tab).
    assert _BASE_TABS == ""
    for name in ("Plan", "Dev", "Review", "Run"):
        assert f'tab name="{name}"' not in _TEMPLATE
    assert 'tab name="Overview" focus=true' in _TEMPLATE


def test_render_workitem_layout_keeps_all_four_phase_subtabs():
    # Removing the base phase tabs must NOT remove the per-item phase sub-tabs.
    kdl = render_workitem_layout(
        name="wi-1", worktree="/wt/a", item_id="wi-1", task_slug="a",
        chat_command=None, default_phase="Dev")
    for phase in ("Plan", "Dev", "Review", "Run"):
        assert f'swap_tiled_layout name="{phase}"' in kdl


def test_render_workitem_layout_has_tab_bar_framing():
    # A focus tab is opened from this layout via `new-tab --layout`; without a
    # default_tab_template it renders with NO tab bar/status bar (you lose the tab
    # bar when switching into a focus tab). It must carry the same frame as the base.
    kdl = render_workitem_layout(
        name="wi-1", worktree="/wt/a", item_id="wi-1", task_slug="a",
        chat_command=None, default_phase="Dev")
    assert "default_tab_template {" in kdl
    assert 'plugin location="zellij:tab-bar"' in kdl
    assert 'plugin location="zellij:status-bar"' in kdl
    # The template must come before the tab it frames.
    assert kdl.index("default_tab_template") < kdl.index('tab name="wi-1"')


def test_workitem_layout_tab_template_matches_base():
    # The per-item frame is the SAME block the base layout uses — keep them in sync
    # via the shared constant so the two can't drift.
    assert _DEFAULT_TAB_TEMPLATE in _TEMPLATE
    kdl = render_workitem_layout(
        name="wi-1", worktree="/wt/a", item_id="wi-1", task_slug="a",
        chat_command=None, default_phase="Dev")
    assert _DEFAULT_TAB_TEMPLATE in kdl


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
    # Base is Overview only; serve appends a Serve tab. No legacy phase tabs.
    assert 'tab name="Overview" focus=true' in kdl
    assert 'tab name="Serve"' in kdl
    for name in ("Plan", "Dev", "Review", "Run"):
        assert f'tab name="{name}"' not in kdl
    assert 'command="mship"' in kdl
    assert 'args "serve";' in kdl
    # Serve tab comes AFTER Overview, and Overview is the launchpad and keeps focus.
    assert kdl.index('tab name="Overview"') < kdl.index('tab name="Serve"')


def test_template_has_overview_launchpad_tab():
    assert 'tab name="Overview" focus=true' in _TEMPLATE
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


def test_layout_launch_execs_zellij(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cap = _capture_launch(monkeypatch, ["layout", "launch"])
    assert cap["file"] == "zellij"
    assert cap["args"] == ["zellij", "--layout", "mothership"]


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
