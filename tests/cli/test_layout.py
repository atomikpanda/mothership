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


def test_base_tabs_has_all_four_tabs():
    for name in ("Plan", "Dev", "Review", "Run"):
        assert f'tab name="{name}"' in _BASE_TABS
    assert 'name="Serve"' not in _BASE_TABS


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


def test_render_serve_layout_no_args_has_base_tabs_plus_serve():
    kdl = render_serve_layout([])
    for name in ("Plan", "Dev", "Review", "Run", "Serve"):
        assert f'tab name="{name}"' in kdl
    assert 'command="mship"' in kdl
    assert 'args "serve";' in kdl
    # Serve tab comes AFTER Run, and Plan keeps focus.
    assert kdl.index('tab name="Run"') < kdl.index('tab name="Serve"')
    assert 'tab name="Plan" focus=true' in kdl


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


# --- pre-existing structural assertion ----------------------------------------

def test_review_tab_has_journal_pane():
    """The Review tab must include a Shell pane and a Journal pane wired to
    `mship view journal --watch`."""
    assert 'tab name="Review"' in _TEMPLATE
    start = _TEMPLATE.index('tab name="Review"')
    end = _TEMPLATE.index('tab name="Run"', start)
    review_block = _TEMPLATE[start:end]

    assert 'name="Shell"' in review_block, review_block
    assert 'name="Journal"' in review_block, review_block
    assert '"view" "journal" "--watch"' in review_block, review_block
