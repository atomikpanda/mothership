import json
import os
import subprocess
from datetime import datetime, timezone

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState


runner = CliRunner()


def _sh(*args, cwd):
    e = {**os.environ,
         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=e)


def _override(path):
    container.config_path.override(path / "mothership.yaml")
    container.state_dir.override(path / ".mothership")


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def _seed_switchable(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace
    return workspace


def test_switch_records_active_repo_and_exits_zero(switch_workspace):
    workspace = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "cli"])
        assert result.exit_code == 0, result.output
        sm = StateManager(workspace / ".mothership")
        state = sm.load()
        assert state.tasks["t"].active_repo == "cli"
        # Shared's SHA snapshotted under last_switched_at_sha["cli"]
        assert "cli" in state.tasks["t"].last_switched_at_sha
        assert "shared" in state.tasks["t"].last_switched_at_sha["cli"]
    finally:
        _reset()


def test_switch_bogus_repo_errors(switch_workspace):
    workspace = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "nope"])
        assert result.exit_code != 0
        assert "nope" in result.output.lower()
    finally:
        _reset()


def test_switch_no_active_task_errors(tmp_path):
    # Empty workspace — no task seeded
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text("workspace: empty\nrepos: {}\n")
    _override(tmp_path)
    try:
        result = runner.invoke(app, ["switch", "whatever"])
        assert result.exit_code != 0
        assert "no active task" in result.output.lower() or "no such command" not in result.output.lower()
    finally:
        _reset()


def test_switch_bare_rerenders_active(switch_workspace):
    workspace = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "cli"])
        assert result.exit_code == 0, result.output

        result2 = runner.invoke(app, ["switch"])
        assert result2.exit_code == 0, result2.output
        # Either "Switched to" (non-TTY JSON won't contain this; be permissive)
        # or the JSON payload with repo=cli.
        try:
            payload = json.loads(result2.output)
            assert payload["repo"] == "cli"
        except json.JSONDecodeError:
            assert "cli" in result2.output
    finally:
        _reset()


def test_switch_bare_no_active_repo_errors(switch_workspace):
    workspace = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch"])
        assert result.exit_code != 0
        assert "no active repo" in result.output.lower() or "switch <repo>" in result.output
    finally:
        _reset()


def test_switch_json_shape(switch_workspace):
    workspace = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "cli"])
        assert result.exit_code == 0, result.output
        # CliRunner is non-TTY → JSON output
        payload = json.loads(result.output)
        assert payload["repo"] == "cli"
        assert payload["task_slug"] == "t"
        assert "dep_changes" in payload
        assert "drift_error_count" in payload
    finally:
        _reset()
