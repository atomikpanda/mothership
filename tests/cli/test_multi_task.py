"""End-to-end multi-task scenarios exercising cwd/env/flag anchoring."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, WorkspaceState


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Minimal mship workspace with no tasks; tests spawn their own."""
    cfg = tmp_path / "mothership.yaml"
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "add", "Taskfile.yml"], cwd=repo_dir, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"],
                   cwd=repo_dir, check=True)
    cfg.write_text(
        "workspace: t\n"
        f"repos:\n  r:\n    path: {repo_dir}\n    type: service\n"
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    StateManager(state_dir).save(WorkspaceState())

    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MSHIP_TASK", raising=False)

    yield tmp_path

    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.log_manager.reset()


def _spawn(runner, desc):
    result = runner.invoke(app, ["spawn", desc, "--skip-setup", "--force-audit", "--bypass-reconcile"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_two_tasks_coexist(workspace):
    runner = CliRunner()
    a = _spawn(runner, "first task")
    b = _spawn(runner, "second task")
    assert a["slug"] != b["slug"]
    state = StateManager(workspace / ".mothership").load()
    assert set(state.tasks.keys()) == {a["slug"], b["slug"]}


def test_status_no_anchor_lists_both_tasks(workspace):
    runner = CliRunner()
    a = _spawn(runner, "first")
    b = _spawn(runner, "second")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert {t["slug"] for t in data["active_tasks"]} == {a["slug"], b["slug"]}


def test_phase_without_anchor_errors_ambiguous(workspace):
    runner = CliRunner()
    _spawn(runner, "first")
    _spawn(runner, "second")
    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code == 1
    combined = (result.stderr or result.output or "").lower()
    assert "multiple active" in combined or "--task" in combined


def test_phase_with_task_flag_transitions_correct_task(workspace):
    runner = CliRunner()
    a = _spawn(runner, "first")
    b = _spawn(runner, "second")
    result = runner.invoke(app, ["phase", "dev", "--task", a["slug"]])
    assert result.exit_code == 0, result.output
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks[a["slug"]].phase == "dev"
    assert state.tasks[b["slug"]].phase == "plan"


def test_env_anchor_scopes_session(workspace, monkeypatch):
    runner = CliRunner()
    a = _spawn(runner, "first")
    _spawn(runner, "second")
    monkeypatch.setenv("MSHIP_TASK", a["slug"])
    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code == 0, result.output
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks[a["slug"]].phase == "dev"


def test_cwd_inside_worktree_resolves(workspace, monkeypatch):
    runner = CliRunner()
    a = _spawn(runner, "first")
    _spawn(runner, "second")
    a_wt = Path(a["worktrees"]["r"])
    monkeypatch.chdir(a_wt)
    result = runner.invoke(app, ["phase", "dev"])
    assert result.exit_code == 0, result.output
    state = StateManager(workspace / ".mothership").load()
    assert state.tasks[a["slug"]].phase == "dev"


def test_journal_writes_to_resolved_task(workspace, monkeypatch):
    runner = CliRunner()
    a = _spawn(runner, "first")
    b = _spawn(runner, "second")
    monkeypatch.chdir(Path(b["worktrees"]["r"]))
    result = runner.invoke(app, ["journal", "hello from B"])
    assert result.exit_code == 0, result.output
    log_dir = workspace / ".mothership" / "logs"
    log_a = log_dir / f"{a['slug']}.md"
    if log_a.exists():
        assert "hello from B" not in log_a.read_text()
    log_b = log_dir / f"{b['slug']}.md"
    assert log_b.exists(), f"expected {log_b} to exist"
    assert "hello from B" in log_b.read_text()
