"""`mship daemon ...` CLI: thin over the supervisor seam. All supervisor
interaction is monkeypatched (`pick_supervisor` → recording fake, the
`tests/cli/test_relay_enroll_server.py` seam style)."""
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import mship
import mship.cli.daemon as daemon_mod
from mship.core.daemon.supervisor import DaemonSupervisorError, SupervisorState

runner = CliRunner()


class FakeSupervisor:
    def __init__(self, *, available=True, state="active", linger="yes"):
        self._available = available
        self._state = state
        self._linger = linger
        self.calls: list[str] = []

    def available(self):
        self.calls.append("available")
        return self._available

    def install(self, argv):
        self.calls.append(f"install:{argv}")

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def restart(self):
        self.calls.append("restart")

    def query(self):
        self.calls.append("query")
        return SupervisorState(self._state)

    def linger_state(self):
        return self._linger

    def logs_tail(self, n):
        self.calls.append(f"logs:{n}")
        return ["line-1", "line-2"]


@pytest.fixture
def cli(monkeypatch, tmp_path):
    fake = FakeSupervisor()
    monkeypatch.setattr(daemon_mod, "pick_supervisor", lambda **kw: fake)
    monkeypatch.setattr(daemon_mod, "resolve_mshipd_argv", lambda: ["/venv/bin/mshipd"])
    monkeypatch.setattr(daemon_mod.Path, "home", classmethod(lambda cls: tmp_path))
    app = typer.Typer()
    daemon_mod.register(app, lambda required=True: None)
    return app, fake


def test_all_subcommands_registered(cli):
    app, _ = cli
    res = runner.invoke(app, ["daemon", "--help"])
    assert res.exit_code == 0
    for cmd in ("install", "start", "stop", "restart", "status", "logs", "run"):
        assert cmd in res.output


def test_install_happy_path(cli):
    app, fake = cli
    res = runner.invoke(app, ["daemon", "install"])
    assert res.exit_code == 0, res.output
    assert any(c.startswith("install:") for c in fake.calls)


def test_install_no_supervisor_fails_loudly(monkeypatch, tmp_path):
    fake = FakeSupervisor(available=False)
    monkeypatch.setattr(daemon_mod, "pick_supervisor", lambda **kw: fake)
    monkeypatch.setattr(daemon_mod, "resolve_mshipd_argv", lambda: ["/venv/bin/mshipd"])
    app = typer.Typer()
    daemon_mod.register(app, lambda required=True: None)
    res = runner.invoke(app, ["daemon", "install"])
    assert res.exit_code == 1
    assert "mship daemon run" in res.output
    assert not any(c.startswith("install:") for c in fake.calls)


def test_restart_consults_blockers_first(cli, monkeypatch):
    app, fake = cli
    monkeypatch.setattr(daemon_mod, "restart_blockers", lambda: ["active unattended worker w-1"])
    res = runner.invoke(app, ["daemon", "restart"])
    assert res.exit_code == 1
    assert "active unattended worker w-1" in res.output
    assert "restart" not in fake.calls  # no supervisor call after refusal


def test_restart_proceeds_when_unblocked(cli):
    app, fake = cli
    res = runner.invoke(app, ["daemon", "restart"])
    assert res.exit_code == 0, res.output
    assert "restart" in fake.calls


def test_status_renders_skew_and_warnings(cli, monkeypatch):
    app, fake = cli
    fake._state = "absent"
    monkeypatch.setattr(
        daemon_mod,
        "probe_daemon",
        lambda **kw: {
            "status": "ok",
            "pid": 7,
            "mship_version": "0.0.1",
            "protocol": 1,
            "uptime_s": 5.0,
            "socket": "/s.sock",
        },
    )
    res = runner.invoke(app, ["daemon", "status"])
    assert res.exit_code == 0, res.output
    assert f"CLI v{mship.__version__}" in res.output
    assert "restart required" in res.output
    assert "outside the supervisor" in res.output


def test_status_rotates_oversized_launchd_capture(cli, tmp_path):
    """Status must bound a pre-main crash loop even when logs is never called."""
    from mship.core.daemon.log_capture import LAUNCHD_CAPTURE_MAX_BYTES
    from mship.core.daemon.paths import daemon_log_dir

    app, _ = cli
    log_dir = daemon_log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    capture = log_dir / "launchd.err.log"
    latest = b"latest startup traceback\n"
    capture.write_bytes(b"x" * (LAUNCHD_CAPTURE_MAX_BYTES + 1) + latest)

    res = runner.invoke(app, ["daemon", "status"])

    assert res.exit_code == 0, res.output
    retired = log_dir / "launchd.err.log.1"
    assert not capture.exists()
    assert retired.read_bytes().endswith(latest)

    # A later launch recreates the active path only after the retired writer
    # exits; the next status can then compact that inactive generation safely.
    capture.write_text("next launch\n")
    res = runner.invoke(app, ["daemon", "status"])
    assert res.exit_code == 0, res.output
    assert retired.stat().st_size == LAUNCHD_CAPTURE_MAX_BYTES
    assert retired.read_bytes().endswith(latest)


def test_logs_prints_tail(cli):
    app, fake = cli
    res = runner.invoke(app, ["daemon", "logs"])
    assert res.exit_code == 0
    assert "line-1" in res.output and "line-2" in res.output


def test_run_invokes_daemon_main(cli, monkeypatch):
    app, fake = cli
    monkeypatch.setattr(daemon_mod, "_daemon_main", lambda: 3)
    res = runner.invoke(app, ["daemon", "run"])
    assert res.exit_code == 3
    assert fake.calls == []  # no supervisor interaction on the foreground path


def test_daemon_status_outside_workspace(tmp_path, monkeypatch):
    """Through the real global app, from a directory with no mothership.yaml:
    no workspace discovery on any daemon path."""
    from mship.cli import app as real_app

    fake = FakeSupervisor()
    monkeypatch.setattr(daemon_mod, "pick_supervisor", lambda **kw: fake)
    monkeypatch.setattr(daemon_mod, "probe_daemon", lambda **kw: None)
    monkeypatch.setattr(daemon_mod.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(real_app, ["daemon", "status"])
    assert res.exit_code == 0, res.output
    assert "not running" in res.output


def test_status_json_mode_emits_json(cli, monkeypatch):
    """`mship --json daemon status` / piped stdout must yield parseable JSON
    (agentic review P2), not the rendered text block."""
    import json as _json

    from mship.cli.output import configure_output, reset_output_settings

    app, fake = cli
    monkeypatch.setattr(daemon_mod, "probe_daemon", lambda **kw: None)
    configure_output(json=True)
    try:
        res = runner.invoke(app, ["daemon", "status"])
    finally:
        reset_output_settings()
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.output)
    assert payload["running"] is False
    assert payload["cli_version"] == mship.__version__
    assert isinstance(payload["lines"], list)
