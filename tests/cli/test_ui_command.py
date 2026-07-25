"""`mship ui` — the console entry point."""
import json

import pytest
from typer.testing import CliRunner

from mship.cli import app
from mship.cli.output import reset_output_settings
from mship.cli.ui import console_url

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean(workspace, monkeypatch):
    """A fresh workspace per test, and a container that actually points at it.

    `get_container` caches through dependency-injector provider overrides, so
    without the reset the second test in this file still resolves the FIRST
    test's temp workspace and cannot find its serve-token. Same reset list as
    tests/cli/test_exec.py's `_reset_container`.
    """
    from mship.cli import container

    def _reset() -> None:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset_override()
        container.config.reset()
        container.state_manager.reset_override()
        container.state_manager.reset()

    _reset()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)
    reset_output_settings()
    yield
    reset_output_settings()
    _reset()


def test_url_carries_the_token_when_auth_is_on():
    assert console_url("127.0.0.1", 47100, "tok") == "http://127.0.0.1:47100/ui?token=tok"


def test_url_omits_the_token_when_the_serve_has_no_auth():
    assert console_url("127.0.0.1", 47100, None) == "http://127.0.0.1:47100/ui"


def test_json_mode_reports_the_url_and_whether_a_token_is_needed(workspace, monkeypatch):
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "envtok")
    result = runner.invoke(app, ["--json", "ui"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert body["url"].endswith("/ui?token=envtok")
    assert body["requires_token"] is True


def test_the_env_token_wins_over_the_file(workspace, monkeypatch):
    (workspace / ".mothership").mkdir(exist_ok=True)
    (workspace / ".mothership" / "serve-token").write_text("file-token\n")
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "env-token")
    body = json.loads(runner.invoke(app, ["--json", "ui"]).stdout)
    assert "env-token" in body["url"] and "file-token" not in body["url"]


def test_the_file_token_is_used_when_no_env_override(workspace):
    (workspace / ".mothership").mkdir(exist_ok=True)
    (workspace / ".mothership" / "serve-token").write_text("file-token\n")
    body = json.loads(runner.invoke(app, ["--json", "ui"]).stdout)
    assert "file-token" in body["url"]


def test_a_corrupt_token_file_degrades_to_no_token(workspace):
    """Reporting must not crash on a non-UTF-8 token file."""
    (workspace / ".mothership").mkdir(exist_ok=True)
    (workspace / ".mothership" / "serve-token").write_bytes(b"\xff\xfe binary")
    body = json.loads(runner.invoke(app, ["--json", "ui"]).stdout)
    assert body["requires_token"] is False


def test_no_browser_prints_the_link_and_never_hangs_without_a_tty(workspace):
    """CliRunner's stdin is not a tty, so the `c` prompt must be skipped rather
    than blocking forever."""
    (workspace / ".mothership").mkdir(exist_ok=True)
    (workspace / ".mothership" / "serve-token").write_text("tok\n")
    result = runner.invoke(app, ["ui", "--no-browser"], env={"MSHIP_JSON": "0"})
    assert result.exit_code == 0
    assert "/ui?token=tok" in result.stdout
    assert "Press" in result.stdout


def test_honours_a_custom_host_and_port(workspace):
    body = json.loads(
        runner.invoke(app, ["--json", "ui", "--host", "0.0.0.0", "--port", "47119"]).stdout
    )
    assert body["url"].startswith("http://0.0.0.0:47119/ui")
