import json
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def _override(audit_workspace):
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(audit_workspace / ".mothership")


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def test_audit_clean_exits_zero(audit_workspace):
    _override(audit_workspace)
    try:
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 0, result.output
        assert "clean" in result.output
    finally:
        _reset()


def test_audit_modified_tracked_exits_one(audit_workspace):
    _override(audit_workspace)
    try:
        (audit_workspace / "cli" / "README.md").write_text("modified\n")
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 1
        assert "dirty_worktree" in result.output
    finally:
        _reset()


def test_audit_json_shape(audit_workspace):
    _override(audit_workspace)
    try:
        (audit_workspace / "cli" / "README.md").write_text("modified\n")
        result = runner.invoke(app, ["audit", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["has_errors"] is True
        assert payload["workspace"] == "audit-test"
        cli_entry = next(r for r in payload["repos"] if r["name"] == "cli")
        codes = {i["code"] for i in cli_entry["issues"]}
        assert "dirty_worktree" in codes
    finally:
        _reset()


def test_audit_repos_filter_unknown(audit_workspace):
    _override(audit_workspace)
    try:
        result = runner.invoke(app, ["audit", "--repos", "cli,nope"])
        assert result.exit_code == 1
        assert "nope" in result.output
    finally:
        _reset()


def test_audit_ignores_task_worktree(audit_workspace, tmp_path):
    """A worktree registered in state.tasks[*].worktrees is not flagged as extra."""
    import subprocess
    import os
    import yaml

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    # Add a git worktree to cli
    clone = audit_workspace / "cli"
    wt = audit_workspace / "cli-wt"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "scratch"],
        cwd=clone, check=True, capture_output=True, env=env,
    )

    # Register it in state as a task worktree
    state_dir = audit_workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_data = {
        "tasks": {
            "t1": {
                "slug": "t1",
                "description": "t",
                "phase": "dev",
                "created_at": "2026-01-01T00:00:00+00:00",
                "branch": "scratch",
                "affected_repos": ["cli"],
                "worktrees": {"cli": str(wt)},
                "pr_urls": {},
                "test_results": {},
            },
        },
    }
    (state_dir / "state.yaml").write_text(yaml.safe_dump(state_data))

    _override(audit_workspace)
    container.state_manager.reset()
    try:
        result = runner.invoke(app, ["audit", "--repos", "cli"])
        assert result.exit_code == 0, result.output
        assert "extra_worktrees" not in result.output
    finally:
        _reset()


def test_audit_still_flags_foreign_worktree(audit_workspace):
    """A worktree NOT in state still fires extra_worktrees."""
    import subprocess
    import os

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    clone = audit_workspace / "cli"
    wt = audit_workspace / "cli-foreign"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "foreign"],
        cwd=clone, check=True, capture_output=True, env=env,
    )

    _override(audit_workspace)
    try:
        result = runner.invoke(app, ["audit", "--repos", "cli"])
        assert result.exit_code == 1
        assert "extra_worktrees" in result.output
        assert "mship prune" in result.output
    finally:
        _reset()


def test_audit_warn_displays_yellow_lane(audit_workspace):
    _override(audit_workspace)
    try:
        (audit_workspace / "cli" / "new.txt").write_text("hi\n")  # untracked → warn
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 0, result.output  # warn does NOT block
        assert "dirty_untracked" in result.output
        assert "warn(s)" in result.output  # footer counter includes warn
    finally:
        _reset()
