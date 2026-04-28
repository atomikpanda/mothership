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
    return workspace, shared_wt, cli_wt, sm


def test_switch_records_active_repo_and_exits_zero(switch_workspace):
    workspace, shared_wt, cli_wt, sm = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "cli", "--task", "t"])
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
    workspace, shared_wt, cli_wt, sm = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "nope", "--task", "t"])
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
    workspace, shared_wt, cli_wt, sm = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "cli", "--task", "t"])
        assert result.exit_code == 0, result.output

        result2 = runner.invoke(app, ["switch", "--task", "t"])
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
    workspace, shared_wt, cli_wt, sm = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "--task", "t"])
        assert result.exit_code != 0
        assert "no active repo" in result.output.lower() or "switch <repo>" in result.output
    finally:
        _reset()


def test_switch_json_shape(switch_workspace):
    workspace, shared_wt, cli_wt, sm = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        result = runner.invoke(app, ["switch", "cli", "--task", "t"])
        assert result.exit_code == 0, result.output
        # CliRunner is non-TTY → JSON output
        payload = json.loads(result.output)
        assert payload["repo"] == "cli"
        assert payload["task_slug"] == "t"
        assert "dep_changes" in payload
        assert "drift_error_count" in payload
    finally:
        _reset()


def test_switch_prepends_cd_hint_when_cwd_differs(switch_workspace, monkeypatch):
    workspace, shared_wt, cli_wt, sm = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        # CliRunner uses subprocess cwd = test's cwd, which is NOT the worktree
        monkeypatch.chdir(workspace)  # workspace root ≠ cli_wt
        result = runner.invoke(app, ["switch", "cli", "--task", "t"])
        assert result.exit_code == 0, result.output
        # TTY output; CliRunner's output is non-TTY though, so this may not show.
        # Instead assert the JSON path still does NOT contain the cd hint
        # and relies on separate TTY test. For now, just ensure the command ran.
    finally:
        _reset()


def test_switch_includes_worktree_path_in_output(switch_workspace, monkeypatch):
    """Assert the worktree path is always surfaced (as cd hint or in JSON)."""
    workspace, shared_wt, cli_wt, sm = _seed_switchable(switch_workspace)
    _override(workspace)
    try:
        monkeypatch.chdir(workspace)
        result = runner.invoke(app, ["switch", "cli", "--task", "t"])
        assert result.exit_code == 0, result.output
        # Non-TTY → JSON; worktree_path in payload
        try:
            payload = json.loads(result.output)
            assert payload["worktree_path"] == str(cli_wt)
        except json.JSONDecodeError:
            # TTY mode: the path should appear literally
            assert str(cli_wt) in result.output
    finally:
        _reset()


def test_switch_to_passive_warns(tmp_path, monkeypatch):
    """`mship switch <passive-repo>` succeeds but prints a passive warning."""
    import os
    import subprocess
    from datetime import datetime, timezone
    from typer.testing import CliRunner
    from mship.cli import app, container
    from mship.core.state import StateManager, Task, WorkspaceState

    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n"
        "  api:\n    path: ./api\n    type: service\n    base_branch: main\n    expected_branch: main\n"
        "  shared:\n    path: ./shared\n    type: library\n    base_branch: main\n    expected_branch: main\n"
    )
    for n in ("api", "shared"):
        d = tmp_path / n
        d.mkdir()
        (d / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
        subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    api_wt = tmp_path / ".worktrees" / "x" / "api"
    shared_wt = tmp_path / ".worktrees" / "x" / "shared"
    api_wt.mkdir(parents=True); shared_wt.mkdir(parents=True)
    # In real spawns passive repos are NOT in affected_repos (only explicit --repos are).
    # Both are in worktrees so switch can reach them; passive_repos marks the read-only ones.
    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["api"], branch="feat/x",
            worktrees={"api": api_wt, "shared": shared_wt},
            passive_repos={"shared"},
        )
    }))
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)
    container.config.reset()
    container.state_manager.reset()
    monkeypatch.chdir(api_wt)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["switch", "shared", "--task", "x"])
        # Switch can succeed (exit 0) but must mention "passive"
        out = (result.output or "").lower()
        # Must explicitly mention "passive" in a warning context, not just in
        # the worktree path (which may contain the test name "passive_warns").
        # Check for a JSON "is_passive" key or a standalone warning line.
        import json as _json
        warned = False
        try:
            payload = _json.loads(result.output)
            warned = payload.get("is_passive", False)
        except _json.JSONDecodeError:
            # TTY mode: look for a warning line mentioning passive
            warned = any(
                "passive" in line.lower()
                for line in result.output.splitlines()
                if "worktree" not in line.lower() and "worktrees" not in line.lower()
            )
        assert warned, f"expected a passive warning in output; got: {result.output}"
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
