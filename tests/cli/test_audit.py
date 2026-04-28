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


def test_audit_includes_passive_repo_drift(tmp_path, monkeypatch):
    """`mship audit` surfaces passive_drift for a passive worktree behind origin."""
    import os
    import subprocess
    from datetime import datetime, timezone
    from typer.testing import CliRunner
    from mship.cli import app, container
    from mship.core.state import StateManager, Task, WorkspaceState

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    bare = tmp_path / "shared.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    src = tmp_path / "shared"
    subprocess.run(["git", "clone", "-q", str(bare), str(src)],
                   check=True, capture_output=True)
    (src / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=src,
                   check=True, capture_output=True, env=env)
    sha1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src,
                          check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "c2"], cwd=src,
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)

    # Passive worktree at the OLD sha1
    passive = tmp_path / ".worktrees" / "x" / "shared"
    subprocess.run(["git", "worktree", "add", "--detach", str(passive), sha1],
                   cwd=src, check=True, capture_output=True)

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
        "    base_branch: main\n    expected_branch: main\n"
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/x",
            worktrees={"shared": passive},
            passive_repos={"shared"},
        )
    }))

    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    monkeypatch.chdir(tmp_path)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["audit", "--json"])
        # The exit code can be 0 or 1 depending on whether the canonical checkout
        # has its own issues. We just want to verify passive_drift surfaces.
        import json as _json
        report = _json.loads(result.stdout)
        # Find the shared repo entry
        shared_entry = next(r for r in report["repos"] if r["name"] == "shared")
        codes = [i["code"] for i in shared_entry["issues"]]
        assert "passive_drift" in codes, f"expected passive_drift in shared issues; got {codes}"
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_audit_no_passive_no_extra_issues(tmp_path, monkeypatch):
    """Smoke: when there are no passive worktrees, audit behaves as before (no extra calls/state)."""
    import os
    import subprocess
    from typer.testing import CliRunner
    from mship.cli import app, container
    from mship.core.state import StateManager, WorkspaceState

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    src = tmp_path / "shared"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True, capture_output=True)
    (src / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=src,
                   check=True, capture_output=True, env=env)

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    StateManager(state_dir).save(WorkspaceState())

    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    monkeypatch.chdir(tmp_path)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["audit", "--json"])
        # JSON valid + has shared repo entry
        import json as _json
        report = _json.loads(result.stdout)
        names = [r["name"] for r in report["repos"]]
        assert "shared" in names
        # No passive_* issues since there are no passive worktrees
        for r in report["repos"]:
            for i in r["issues"]:
                assert not i["code"].startswith("passive_"), f"unexpected passive issue: {i}"
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
