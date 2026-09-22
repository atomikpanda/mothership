from datetime import datetime, timezone

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState


runner = CliRunner()


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _source_repo(root: Path) -> Path:
    src = root / "src"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], src)
    (src / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    _git(["add", "."], src)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"], src)
    return src


def _ws(root: Path, body: str) -> Path:
    ws = root / "ws"
    ws.mkdir()
    (ws / "mothership.yaml").write_text(body)
    (ws / ".mothership").mkdir()
    return ws


def _configure(ws: Path):
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(ws / "mothership.yaml")
    container.state_dir.override(ws / ".mothership")


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_bootstrap_non_tty_is_pure_json(tmp_path):
    src = _source_repo(tmp_path)
    ws = _ws(tmp_path,
             "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
             f"    url: file://{src}\n")
    _configure(ws)
    try:
        result = runner.invoke(app, ["bootstrap"])
        data = json.loads(result.stdout)  # must be pure JSON
        assert any(m["name"] == "lib" and m["status"] == "cloned"
                   for m in data["members"])
        assert result.exit_code == 0
        assert (ws / "lib" / "Taskfile.yml").exists()
    finally:
        _reset()


def test_bootstrap_exit_nonzero_on_member_error(tmp_path):
    ws = _ws(tmp_path,
             "workspace: w\nrepos:\n  bad:\n    path: bad\n    type: library\n")
    _configure(ws)
    try:
        result = runner.invoke(app, ["bootstrap"])
        data = json.loads(result.stdout)
        assert data["members"][0]["status"] == "error"
        assert result.exit_code == 1
        assert data["errors"] == 1
        assert data["warnings"] == []
    finally:
        _reset()


def test_bootstrap_unknown_repo_filter_errors_cleanly(tmp_path):
    src = _source_repo(tmp_path)
    ws = _ws(tmp_path,
             "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
             f"    url: file://{src}\n")
    _configure(ws)
    try:
        result = runner.invoke(app, ["bootstrap", "--repos", "typo,lib"])
        # Actionable error, not a raw KeyError traceback.
        assert result.exit_code == 1
        assert "typo" in result.output
        assert "Unknown repo" in result.output
    finally:
        _reset()


def test_bootstrap_relay_pairing_error_exit_nonzero(tmp_path):
    ws = _ws(tmp_path,
             "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n")
    _configure(ws)
    try:
        result = runner.invoke(app, ["bootstrap", "--run-token", "rt-1"])
        assert result.exit_code == 1
        assert "relay-url" in result.output.lower()
    finally:
        _reset()


def test_bootstrap_relay_forwards_both_flags_to_core(tmp_path, monkeypatch):
    import mship.core.bootstrap as bmod
    from mship.core.bootstrap import BootstrapReport

    captured = {}

    def fake_bootstrap(config_path, shell, *, state_dir, repos=None, token=None,
                       relay_url=None, run_token=None):
        captured.update(relay_url=relay_url, run_token=run_token)
        return BootstrapReport(members=(), doctor_ok=None)

    monkeypatch.setattr(bmod, "bootstrap", fake_bootstrap)

    ws = _ws(tmp_path,
             "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n")
    _configure(ws)
    try:
        result = runner.invoke(
            app,
            ["bootstrap", "--relay-url", "https://relay.example", "--run-token", "rt-1"],
        )
        assert result.exit_code == 0, result.output
        assert captured == {"relay_url": "https://relay.example", "run_token": "rt-1"}
    finally:
        _reset()


def test_bootstrap_bare_remote_with_following_options_reaches_task_validation(tmp_path):
    ws = _ws(
        tmp_path,
        """\
workspace: w
repos:
  app:
    path: app
    type: service
    host_tools:
      mise:
        manifest: .mise.toml
""",
    )
    app_dir = ws / "app"
    app_dir.mkdir()
    (app_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    _configure(ws)
    try:
        result = runner.invoke(
            app,
            [
                "bootstrap",
                "--host-tools",
                "--remote",
                "--task",
                "no-such-task",
                "--repo",
                "app",
            ],
        )
        assert result.exit_code == 1
        assert "--task" in result.output and "active" in result.output.lower()
    finally:
        _reset()


def test_bootstrap_trailing_bare_remote_reaches_host_tools_validation(tmp_path):
    ws = _ws(tmp_path, "workspace: w\nrepos: {}\n")
    _configure(ws)
    try:
        result = runner.invoke(app, ["bootstrap", "--host-tools", "--remote"])
        assert result.exit_code == 1
        assert "--task" in result.output and "--repo" in result.output
    finally:
        _reset()


def test_bootstrap_explicit_remote_role_reaches_run_host_validation(tmp_path):
    ws = _ws(
        tmp_path,
        """\
workspace: w
run_hosts: [default]
repos:
  app:
    path: app
    type: service
    host_tools:
      mise:
        manifest: .mise.toml
""",
    )
    app_dir = ws / "app"
    app_dir.mkdir()
    (app_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    _configure(ws)
    try:
        StateManager(ws / ".mothership").save(
            WorkspaceState(
                tasks={
                    "task-1": Task(
                        slug="task-1",
                        description="remote host validation",
                        phase="dev",
                        created_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                        affected_repos=["app"],
                        worktrees={"app": app_dir},
                        branch="feat/task-1",
                    )
                }
            )
        )
        result = runner.invoke(
            app,
            [
                "bootstrap",
                "--host-tools",
                "--remote=studio",
                "--task",
                "task-1",
                "--repo",
                "app",
            ],
        )
        assert result.exit_code == 1
        assert "studio" in result.output
    finally:
        _reset()
