"""`mship daemon ...` CLI: thin over the supervisor seam. All supervisor
interaction is monkeypatched (`pick_supervisor` → recording fake, the
`tests/cli/test_relay_enroll_server.py` seam style)."""
from pathlib import Path
from click import unstyle


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


def test_restart_persists_shell_host_token_before_supervisor_restart(
    cli, tmp_path, monkeypatch
):
    from mship.core.daemon.paths import daemon_state_dir

    app, fake = cli
    observed = {}
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "shell-token")

    def restart():
        observed["token"] = (daemon_state_dir(tmp_path) / "serve-token").read_text().strip()

    fake.restart = restart
    res = runner.invoke(app, ["daemon", "restart"])

    assert res.exit_code == 0, res.output
    assert observed["token"] == "shell-token"


def test_restart_failure_restores_previous_host_token(cli, tmp_path, monkeypatch):
    from mship.core.daemon.host_app import persist_host_token
    from mship.core.daemon.paths import daemon_state_dir

    app, fake = cli
    persist_host_token(tmp_path, "previous-token")
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "new-token")

    def fail_restart():
        raise DaemonSupervisorError("restart failed")

    fake.restart = fail_restart
    res = runner.invoke(app, ["daemon", "restart"])

    assert res.exit_code == 1
    assert (daemon_state_dir(tmp_path) / "serve-token").read_text().strip() == "previous-token"


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


def test_status_counts_missing_as_discovered_not_degraded(cli, tmp_path):
    from datetime import datetime, timezone

    from mship.core.daemon.paths import registry_path
    from mship.core.daemon.registry import RegistryStore, WorkspaceEntry

    app, _ = cli
    now = datetime.now(timezone.utc)
    store = RegistryStore(registry_path(tmp_path))

    def seed(state):
        state.entries = [
            WorkspaceEntry(
                id=f"ws-{entry_state}",
                name=entry_state,
                path=str(tmp_path / entry_state),
                config_path=str(tmp_path / entry_state / "mothership.yaml"),
                state=entry_state,
                first_seen=now,
                last_seen=now,
            )
            for entry_state in ("healthy", "degraded", "missing")
        ]

    store.mutate(seed)
    res = runner.invoke(app, ["daemon", "status"])

    assert res.exit_code == 0, res.output
    assert "workspaces: 3 discovered (1 degraded)" in res.output


def test_status_reports_registry_read_error_without_hiding_daemon_state(
    cli, tmp_path, monkeypatch
):
    from mship.core.daemon.paths import registry_path

    app, _ = cli
    path = registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"entries": []}')
    real_read_text = Path.read_text

    def fail_registry_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_registry_read)
    res = runner.invoke(app, ["daemon", "status"])

    assert res.exit_code == 0, res.output
    assert "daemon: not running" in res.output
    assert "supervisor: active" in res.output
    assert "workspaces: registry not loaded" in res.output
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


def test_install_seeds_scan_roots_and_serve(cli, tmp_path):
    from mship.core.daemon.registry import load_daemon_config

    app, fake = cli
    src, work = tmp_path / "src", tmp_path / "work"
    src.mkdir()
    work.mkdir()
    res = runner.invoke(app, [
        "daemon", "install",
        "--scan-root", str(src),
        "--scan-root", str(work),
        "--serve", "127.0.0.1:47190",
    ])
    assert res.exit_code == 0, res.output
    cfg = load_daemon_config(tmp_path)
    assert cfg.scan_roots == sorted([str(src), str(work)])
    assert cfg.serve == {"host": "127.0.0.1", "port": 47190}


def test_install_persists_config_before_supervisor_bootstrap(cli, tmp_path):
    from mship.core.daemon.registry import load_daemon_config

    app, fake = cli
    observed = {}
    src = tmp_path / "src"
    src.mkdir()

    def install(_argv):
        observed["config"] = load_daemon_config(tmp_path)

    fake.install = install
    res = runner.invoke(app, [
        "daemon", "install", "--scan-root", str(src),
        "--serve", "127.0.0.1:47190",
    ])

    assert res.exit_code == 0, res.output
    assert observed["config"].scan_roots == [str(src)]
    assert observed["config"].serve == {"host": "127.0.0.1", "port": 47190}


def test_install_persists_shell_host_token_before_supervisor_bootstrap(
    cli, tmp_path, monkeypatch
):
    from mship.core.daemon.paths import daemon_state_dir

    app, fake = cli
    fake._state = "absent"
    observed = {}
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "shell-token")

    def install(_argv):
        observed["token"] = (daemon_state_dir(tmp_path) / "serve-token").read_text().strip()

    fake.install = install
    res = runner.invoke(app, ["daemon", "install"])

    assert res.exit_code == 0, res.output
    assert observed["token"] == "shell-token"


def test_start_persists_shell_host_token_before_supervisor_start(
    cli, tmp_path, monkeypatch
):
    from mship.core.daemon.paths import daemon_state_dir

    app, fake = cli
    fake._state = "absent"
    observed = {}
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "  shell-token \n")

    def start():
        observed["token"] = (daemon_state_dir(tmp_path) / "serve-token").read_bytes()

    fake.start = start
    res = runner.invoke(app, ["daemon", "start"])

    assert res.exit_code == 0, res.output
    assert observed["token"] == b"shell-token\n"


def test_start_persists_github_app_credentials_before_supervisor_start(
    cli, tmp_path, monkeypatch
):
    from mship.core.daemon.host_app import load_gh_app_credentials

    app, fake = cli
    fake._state = "absent"
    key_path = tmp_path / "app.pem"
    key_path.write_text("PRIVATE KEY")
    monkeypatch.setenv("MSHIP_GH_APP_ID", "123")
    monkeypatch.setenv("MSHIP_GH_APP_KEY", str(key_path))
    observed = {}

    def start():
        observed["credentials"] = load_gh_app_credentials(tmp_path, env={})

    fake.start = start
    res = runner.invoke(app, ["daemon", "start"])

    assert res.exit_code == 0, res.output
    assert observed["credentials"] == ("123", "PRIVATE KEY")


def test_start_failure_restores_previous_github_app_credentials(
    cli, tmp_path, monkeypatch
):
    from mship.core.daemon.host_app import (
        load_gh_app_credentials,
        persist_gh_app_credentials,
    )

    app, fake = cli
    fake._state = "absent"
    persist_gh_app_credentials(tmp_path, "old-id", "OLD KEY")
    key_path = tmp_path / "new.pem"
    key_path.write_text("NEW KEY")
    monkeypatch.setenv("MSHIP_GH_APP_ID", "new-id")
    monkeypatch.setenv("MSHIP_GH_APP_KEY", str(key_path))

    def fail_start():
        raise DaemonSupervisorError("start failed")

    fake.start = fail_start
    res = runner.invoke(app, ["daemon", "start"])

    assert res.exit_code == 1
    assert load_gh_app_credentials(tmp_path, env={}) == ("old-id", "OLD KEY")


@pytest.mark.parametrize("override", ["host-token", "github-app"])
def test_active_install_rejects_credential_override_without_persisting(
    override, cli, tmp_path, monkeypatch
):
    from mship.core.daemon.host_app import (
        ensure_host_token,
        load_gh_app_credentials,
        persist_gh_app_credentials,
        persist_host_token,
    )

    app, fake = cli
    persist_host_token(tmp_path, "old-token")
    persist_gh_app_credentials(tmp_path, "old-id", "OLD KEY")
    if override == "host-token":
        monkeypatch.setenv("MSHIP_SERVE_TOKEN", "new-token")
    else:
        key_path = tmp_path / "new.pem"
        key_path.write_text("NEW KEY")
        monkeypatch.setenv("MSHIP_GH_APP_ID", "new-id")
        monkeypatch.setenv("MSHIP_GH_APP_KEY", str(key_path))

    res = runner.invoke(app, ["daemon", "install"])

    assert res.exit_code == 1
    assert "already active" in res.output
    assert "restart" in res.output
    assert ensure_host_token(tmp_path, env={}) == "old-token"
    assert load_gh_app_credentials(tmp_path, env={}) == ("old-id", "OLD KEY")
    assert not any(call.startswith("install") for call in fake.calls)


def test_active_start_rejects_host_token_override_without_persisting(
    cli, tmp_path, monkeypatch
):
    from mship.core.daemon.host_app import persist_host_token
    from mship.core.daemon.paths import daemon_state_dir

    app, fake = cli
    persist_host_token(tmp_path, "old-token")
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "new-token")

    res = runner.invoke(app, ["daemon", "start"])

    assert res.exit_code == 1
    assert "already active" in res.output
    assert "restart" in res.output
    assert (daemon_state_dir(tmp_path) / "serve-token").read_text().strip() == "old-token"
    assert "start" not in fake.calls


def test_active_start_rejects_github_app_override_without_persisting(
    cli, tmp_path, monkeypatch
):
    from mship.core.daemon.host_app import (
        load_gh_app_credentials,
        persist_gh_app_credentials,
    )

    app, fake = cli
    persist_gh_app_credentials(tmp_path, "old-id", "OLD KEY")
    key_path = tmp_path / "new.pem"
    key_path.write_text("NEW KEY")
    monkeypatch.setenv("MSHIP_GH_APP_ID", "new-id")
    monkeypatch.setenv("MSHIP_GH_APP_KEY", str(key_path))

    res = runner.invoke(app, ["daemon", "start"])

    assert res.exit_code == 1
    assert "already active" in res.output
    assert "restart" in res.output
    assert load_gh_app_credentials(tmp_path, env={}) == ("old-id", "OLD KEY")
    assert "start" not in fake.calls


@pytest.mark.parametrize("command", ["install", "start", "restart"])
def test_partial_github_app_override_fails_before_supervisor(
    command, cli, tmp_path, monkeypatch
):
    from mship.core.daemon.host_app import (
        load_gh_app_credentials,
        persist_gh_app_credentials,
    )

    app, fake = cli
    persist_gh_app_credentials(tmp_path, "old-id", "OLD KEY")
    monkeypatch.setenv("MSHIP_GH_APP_ID", "new-id")
    monkeypatch.delenv("MSHIP_GH_APP_KEY", raising=False)

    res = runner.invoke(app, ["daemon", command])

    assert res.exit_code == 1
    assert "must be set together" in res.output
    assert (
        "unset both MSHIP_GH_APP_ID and MSHIP_GH_APP_KEY"
        in res.output
    )
    assert not any(call.startswith(command) for call in fake.calls)
    assert load_gh_app_credentials(tmp_path, env={}) == ("old-id", "OLD KEY")


def test_install_invalid_github_app_key_does_not_mutate_config(
    cli, tmp_path, monkeypatch
):
    from mship.core.daemon.registry import (
        DaemonConfig,
        load_daemon_config,
        save_daemon_config,
    )

    app, fake = cli
    previous = DaemonConfig(scan_roots=["/existing"], max_depth=4)
    save_daemon_config(tmp_path, previous)
    monkeypatch.setenv("MSHIP_GH_APP_ID", "new-id")
    monkeypatch.setenv("MSHIP_GH_APP_KEY", str(tmp_path / "missing.pem"))

    res = runner.invoke(
        app, ["daemon", "install", "--scan-root", str(tmp_path / "new")]
    )

    assert res.exit_code == 1
    assert load_daemon_config(tmp_path) == previous
    assert not any(call.startswith("install:") for call in fake.calls)


def test_install_failure_restores_previous_config(cli, tmp_path):
    from mship.core.daemon.registry import (
        DaemonConfig,
        load_daemon_config,
        save_daemon_config,
    )

    app, fake = cli
    existing = tmp_path / "existing"
    new = tmp_path / "new"
    existing.mkdir()
    new.mkdir()
    previous = DaemonConfig(scan_roots=[str(existing)], max_depth=4)
    save_daemon_config(tmp_path, previous)

    def fail_install(_argv):
        raise DaemonSupervisorError("bootstrap failed")

    fake.install = fail_install
    res = runner.invoke(app, ["daemon", "install", "--scan-root", str(new)])

    assert res.exit_code == 1
    assert load_daemon_config(tmp_path) == previous


def test_install_filesystem_failure_restores_previous_config(cli, tmp_path):
    from mship.core.daemon.registry import (
        DaemonConfig,
        load_daemon_config,
        save_daemon_config,
    )

    app, fake = cli
    existing = tmp_path / "existing"
    new = tmp_path / "new"
    existing.mkdir(exist_ok=True)
    new.mkdir(exist_ok=True)
    previous = DaemonConfig(scan_roots=[str(existing)], max_depth=4)
    save_daemon_config(tmp_path, previous)

    def fail_install(_argv):
        raise OSError("unit write failed")

    fake.install = fail_install
    res = runner.invoke(app, ["daemon", "install", "--scan-root", str(new)])

    assert res.exit_code == 1
    assert load_daemon_config(tmp_path) == previous


def test_install_without_serve_leaves_null_bind(cli, tmp_path):
    from mship.core.daemon.registry import load_daemon_config

    app, fake = cli
    src = tmp_path / "src"
    src.mkdir()
    res = runner.invoke(app, ["daemon", "install", "--scan-root", str(src)])
    assert res.exit_code == 0, res.output
    assert load_daemon_config(tmp_path).serve is None


def test_install_seeds_relay_beside_scan_roots_and_serve(cli, tmp_path):
    from mship.core.daemon.registry import load_daemon_config

    app, fake = cli
    src = tmp_path / "src"
    src.mkdir()
    res = runner.invoke(app, [
        "daemon", "install",
        "--scan-root", str(src),
        "--serve", "127.0.0.1:47190",
        "--relay", "relay.example.com",
    ])
    assert res.exit_code == 0, res.output
    cfg = load_daemon_config(tmp_path)
    assert cfg.scan_roots == [str(src)]
    assert cfg.serve == {"host": "127.0.0.1", "port": 47190}
    assert cfg.relay == {"host": "relay.example.com"}


def test_bare_relay_install_succeeds_when_serve_was_set_earlier(cli, tmp_path):
    """Incremental provisioning: `--serve` yesterday, `--relay` today. The
    config MERGES, so validating the flag instead of the merged result would
    reject the normal path."""
    from mship.core.daemon.registry import load_daemon_config

    app, fake = cli
    assert runner.invoke(
        app, ["daemon", "install", "--serve", "127.0.0.1:47190"]
    ).exit_code == 0

    res = runner.invoke(app, ["daemon", "install", "--relay", "relay.example.com"])

    assert res.exit_code == 0, res.output
    cfg = load_daemon_config(tmp_path)
    assert cfg.relay == {"host": "relay.example.com"}
    assert cfg.serve == {"host": "127.0.0.1", "port": 47190}


def test_install_rejects_relay_when_merged_config_has_no_serve_bind(cli, tmp_path):
    """A tunnel forwards a local port; with no bind there is nothing to
    forward, and the host would register itself as reachable and 502."""
    from mship.core.daemon.registry import load_daemon_config

    app, fake = cli
    res = runner.invoke(app, ["daemon", "install", "--relay", "relay.example.com"])

    assert res.exit_code == 1
    assert "--serve" in res.output
    assert load_daemon_config(tmp_path).relay is None
    assert not any(call.startswith("install:") for call in fake.calls)


@pytest.mark.parametrize("relay", ["", "   "])
def test_install_rejects_an_empty_relay_host(cli, tmp_path, relay):
    from mship.core.daemon.registry import load_daemon_config

    app, fake = cli
    res = runner.invoke(app, [
        "daemon", "install", "--serve", "127.0.0.1:47190", "--relay", relay,
    ])
    assert res.exit_code == 1
    assert "HOST" in res.output
    assert load_daemon_config(tmp_path).relay is None
    assert not any(call.startswith("install:") for call in fake.calls)


def test_install_strips_surrounding_whitespace_from_the_relay_host(cli, tmp_path):
    from mship.core.daemon.registry import load_daemon_config

    app, _fake = cli
    res = runner.invoke(app, [
        "daemon", "install", "--serve", "127.0.0.1:47190",
        "--relay", "  relay.example.com  ",
    ])
    assert res.exit_code == 0, res.output
    assert load_daemon_config(tmp_path).relay == {"host": "relay.example.com"}


def test_install_help_documents_that_relay_needs_a_restart(cli):
    app, _fake = cli
    res = runner.invoke(app, ["daemon", "install", "--help"])
    assert res.exit_code == 0
    # Rich wraps option help across lines inside a box and may emit ANSI
    # between words on newer Python/Rich combinations.
    text = " ".join(unstyle(res.output).replace("│", " ").split())
    assert "a changed relay takes effect on `mship daemon restart`" in text


@pytest.mark.parametrize("serve", ["nonsense", "127.0.0.1:0", "127.0.0.1:65536"])
def test_install_rejects_malformed_serve(cli, serve):
    app, fake = cli
    res = runner.invoke(app, ["daemon", "install", "--serve", serve])
    assert res.exit_code == 1
    assert "HOST:PORT" in res.output
    assert not any(c.startswith("install:") for c in fake.calls)


def test_install_rejects_relative_scan_root(cli):
    app, fake = cli
    res = runner.invoke(app, ["daemon", "install", "--scan-root", "relative/path"])
    assert res.exit_code == 1
    assert "absolute" in res.output


def test_install_rejects_missing_scan_root(cli, tmp_path):
    app, fake = cli
    missing = tmp_path / "unmounted"

    res = runner.invoke(
        app, ["daemon", "install", "--scan-root", str(missing)]
    )

    assert res.exit_code == 1


    assert str(missing) in res.output
    assert not any(call.startswith("install:") for call in fake.calls)

@pytest.mark.parametrize("command", ["install", "start", "restart"])
def test_daemon_lifecycle_rejects_malformed_config_before_supervisor(
    command, cli, tmp_path
):
    from mship.core.daemon.paths import daemon_config_path

    app, fake = cli
    path = daemon_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("scan_roots: [unterminated")

    res = runner.invoke(app, ["daemon", command])

    assert res.exit_code == 1
    assert "invalid daemon config" in res.output
    assert str(path) in res.output
    assert not any(call.startswith(command) for call in fake.calls)


@pytest.mark.parametrize("command", ["install", "start", "restart"])
def test_starting_command_rejects_unavailable_configured_scan_root(
    command, cli, tmp_path
):
    from mship.core.daemon.registry import DaemonConfig, save_daemon_config

    app, fake = cli
    missing = tmp_path / "unmounted"
    save_daemon_config(
        tmp_path, DaemonConfig(scan_roots=[str(missing)])
    )

    res = runner.invoke(app, ["daemon", command])

    assert res.exit_code == 1
    assert str(missing) in res.output
    assert not any(call.startswith(command) for call in fake.calls)


@pytest.mark.parametrize("command", ["install", "start"])
@pytest.mark.parametrize("symlink_position", ["final", "ancestor"])
def test_daemon_rejects_symlinked_scan_root_component(
    command, symlink_position, cli, tmp_path
):
    from mship.core.daemon.registry import DaemonConfig, save_daemon_config

    app, fake = cli
    outside = tmp_path / "outside"
    nested = outside / "nested"
    nested.mkdir(parents=True)
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    linked = trusted / "link"
    linked.symlink_to(outside, target_is_directory=True)
    configured = (
        linked if symlink_position == "final" else linked / "nested"
    )

    args = ["daemon", command]
    if command == "install":
        args.extend(["--scan-root", str(configured)])
    else:
        save_daemon_config(
            tmp_path,
            DaemonConfig(scan_roots=[str(configured)]),
        )

    res = runner.invoke(app, args)

    assert res.exit_code == 1
    assert str(linked) in res.output
    assert str(configured) in res.output
    assert not any(call.startswith(command) for call in fake.calls)


@pytest.mark.parametrize("command", ["install", "start", "restart"])
def test_blank_host_token_override_fails_before_supervisor(
    command, cli, monkeypatch
):
    app, fake = cli
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", " \t\n")

    res = runner.invoke(app, ["daemon", command])

    assert res.exit_code == 1
    assert "MSHIP_SERVE_TOKEN must not be blank" in res.output
    assert not any(call.startswith(command) for call in fake.calls)


@pytest.mark.parametrize("command", ["install", "start", "restart"])
def test_blank_github_app_key_fails_before_supervisor(
    command, cli, tmp_path, monkeypatch
):
    from mship.core.daemon.host_app import (
        load_gh_app_credentials,
        persist_gh_app_credentials,
    )

    app, fake = cli
    persist_gh_app_credentials(tmp_path, "old-id", "OLD KEY")
    key_path = tmp_path / "blank.pem"
    key_path.write_text("  \n")
    monkeypatch.setenv("MSHIP_GH_APP_ID", "new-id")
    monkeypatch.setenv("MSHIP_GH_APP_KEY", str(key_path))

    res = runner.invoke(app, ["daemon", command])

    assert res.exit_code == 1
    assert str(key_path) in res.output
    assert not any(call.startswith(command) for call in fake.calls)
    assert load_gh_app_credentials(tmp_path, env={}) == ("old-id", "OLD KEY")


@pytest.mark.parametrize("command", ["install", "start", "restart"])
def test_daemon_command_rejects_incomplete_persisted_github_app(
    command, cli, tmp_path
):
    from mship.core.daemon.host_app import _credential_paths
    from mship.core.daemon.registry import (
        DaemonConfig,
        load_daemon_config,
        save_daemon_config,
    )

    app, fake = cli
    previous_config = DaemonConfig()
    save_daemon_config(tmp_path, previous_config)
    _token_path, app_id_path, app_key_path = _credential_paths(tmp_path)
    app_id_path.parent.mkdir(parents=True, exist_ok=True)
    app_id_path.write_text("123\n")

    args = ["daemon", command]
    if command == "install":
        new_root = tmp_path / "new-root"
        new_root.mkdir()
        args.extend(["--scan-root", str(new_root)])

    res = runner.invoke(app, args)

    assert res.exit_code == 1
    assert str(app_key_path) in res.output
    assert not any(call.startswith(command) for call in fake.calls)
    assert load_daemon_config(tmp_path) == previous_config


@pytest.mark.parametrize("command", ["install", "start", "restart"])
def test_daemon_command_reports_persisted_github_app_read_error(
    command, cli, tmp_path, monkeypatch
):
    from mship.core.daemon.host_app import (
        _credential_paths,
        persist_gh_app_credentials,
    )

    app, fake = cli
    persist_gh_app_credentials(tmp_path, "123", "PRIVATE KEY")
    _token_path, _app_id_path, app_key_path = _credential_paths(tmp_path)
    real_read_text = Path.read_text

    def fail_key_read(self, *args, **kwargs):
        if self == app_key_path:
            raise PermissionError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_key_read)
    res = runner.invoke(app, ["daemon", command])

    assert res.exit_code == 1
    assert str(app_key_path) in res.output
    assert not any(call.startswith(command) for call in fake.calls)


# --- tunnel state + identity recovery on the CLI (#471 Task 9) --------------

TUNNEL_HEALTH = {
    "status": "ok",
    "pid": 7,
    "mship_version": mship.__version__,
    "protocol": 3,
    "uptime_s": 5.0,
    "socket": "/s.sock",
    "tunnel": {
        "state": "online",
        "subdomain": "hst-abc",
        "public_url": "https://hst-abc.relay.example",
        "restarts": 0,
        "last_error": None,
        "clock_skew_seconds": 3600.0,
    },
}


def test_status_renders_the_tunnel_block(cli, monkeypatch):
    app, _ = cli
    monkeypatch.setattr(daemon_mod, "probe_daemon", lambda **kw: TUNNEL_HEALTH)
    res = runner.invoke(app, ["daemon", "status"])
    assert res.exit_code == 0, res.output
    assert "tunnel: online https://hst-abc.relay.example (0 restarts)" in res.output


def test_status_json_carries_the_tunnel_and_skew_first_class(cli, monkeypatch):
    """AC12: `mship --json daemon status` is what the manual checklist reads a
    non-zero `clock_skew_seconds` out of — the rendered text is not parseable."""
    import json as _json

    from mship.cli.output import configure_output, reset_output_settings

    app, _ = cli
    monkeypatch.setattr(daemon_mod, "probe_daemon", lambda **kw: TUNNEL_HEALTH)
    configure_output(json=True)
    try:
        res = runner.invoke(app, ["daemon", "status"])
    finally:
        reset_output_settings()
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.output)
    assert payload["tunnel"] == TUNNEL_HEALTH["tunnel"]
    assert payload["clock_skew_seconds"] == 3600.0


@pytest.fixture
def no_keygen(monkeypatch, tmp_path):
    """A relay keypair that appears on demand, so nothing here spawns
    ssh-keygen (the `test_relay_link.py::_seed_key` discipline)."""
    from itertools import count

    from mship.core.relay import keys

    minted = count()

    def fake_ensure(home, runner=None):
        path = keys.relay_key_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # A DIFFERENT key each time, like ssh-keygen: a rotation that
            # reproduced the same bytes would leave the twin's copy working.
            nth = next(minted)
            path.write_text(f"PRIVATE-{nth}")
            path.with_name(path.name + ".pub").write_text(
                "ssh-ed25519 " + f"{nth}".rjust(68, "A") + " mship-relay\n"
            )
        return path

    monkeypatch.setattr(keys, "ensure_relay_key", fake_ensure)
    return fake_ensure


def _identity(home: Path) -> dict:
    import json as _json

    from mship.core.daemon.paths import host_identity_path

    return _json.loads(host_identity_path(home).read_text())


def test_reidentify_mints_a_new_id_rotates_the_key_and_asks_for_a_restart(
    cli, tmp_path, no_keygen
):
    """The cloned-VM recovery an operator forces by hand: the twin's copy of the
    relay key must stop working, so a new id alone is not enough."""
    from mship.core.daemon.identity import ensure_host_identity
    from mship.core.relay.keys import relay_key_path

    app, _ = cli
    no_keygen(tmp_path)
    original = ensure_host_identity(tmp_path, fingerprint="fp-1")
    original_key = relay_key_path(tmp_path).read_text()

    res = runner.invoke(app, ["daemon", "reidentify"])

    assert res.exit_code == 0, res.output
    fresh = _identity(tmp_path)
    assert fresh["host_id"] != original.host_id
    assert fresh["cloned_from"] == original.host_id
    assert relay_key_path(tmp_path).read_text() != original_key
    assert any(
        p.name.startswith("relay_ed25519.pre-reidentify-")
        for p in relay_key_path(tmp_path).parent.iterdir()
    )
    assert "restart" in res.output.lower()


def test_reidentify_prints_the_new_subdomain(cli, tmp_path, no_keygen):
    """The operator's next move is `mship relay approve` on the relay host, and
    the subdomain is how they recognise this host there."""
    from mship.core.daemon.relay_link import host_subdomain_for

    app, _ = cli
    no_keygen(tmp_path)
    res = runner.invoke(app, ["daemon", "reidentify"])

    assert res.exit_code == 0, res.output
    assert host_subdomain_for(tmp_path, _identity(tmp_path)["host_id"]) in res.output
    assert "--store-dir <relay-store>" in res.output
    assert "--pubkeys-dir <relay-pubkeys>" in res.output
    assert "pgrep -af" in res.output


def test_reidentify_keep_identity_adopts_the_fingerprint_without_reminting(
    cli, tmp_path, no_keygen, monkeypatch
):
    """AC4a: the operator asserting 'this IS still the same host' after a
    re-image — `on_mismatch="keep"`, which nothing else in the product calls."""
    from mship.core.daemon import identity as identity_mod
    from mship.core.daemon.identity import ensure_host_identity
    from mship.core.relay.keys import relay_key_path

    app, _ = cli
    no_keygen(tmp_path)
    original = ensure_host_identity(tmp_path, fingerprint="fp-old")
    original_key = relay_key_path(tmp_path).read_text()
    monkeypatch.setattr(identity_mod, "machine_fingerprint", lambda *a, **kw: "fp-new")

    res = runner.invoke(app, ["daemon", "reidentify", "--keep-identity"])

    assert res.exit_code == 0, res.output
    kept = _identity(tmp_path)
    assert kept["host_id"] == original.host_id
    assert kept["fingerprint"] == "fp-new"
    assert relay_key_path(tmp_path).read_text() == original_key
    assert "fp-new" in res.output
    assert "restart" in res.output.lower()
