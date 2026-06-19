"""Tests for `mship dispatch` CLI."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState, DependencyEdge


runner = CliRunner()


def _bootstrap(tmp_path: Path, worktrees: dict[str, Path], active_repo: str | None = None) -> tuple[Path, Path]:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=list(worktrees.keys()),
        worktrees=worktrees, branch="feat/t",
        base_branch="main", active_repo=active_repo,
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"t": task}))
    return cfg, state_dir


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.log_manager.reset()


def test_dispatch_single_repo_task_prints_prompt(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "do the thing"])
        assert result.exit_code == 0, result.output
        assert f"cd {wt}" in result.output
        assert "> do the thing" in result.output
        assert "slug:** t" in result.output or "slug: t" in result.output
    finally:
        _reset()


def test_dispatch_multi_repo_no_active_errors(tmp_path: Path):
    cfg, state_dir = _bootstrap(tmp_path, {
        "a": tmp_path / "a", "b": tmp_path / "b",
    })
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "x"])
        assert result.exit_code == 1
        assert "affects 2 repos" in result.output
    finally:
        _reset()


def test_dispatch_multi_repo_with_repo_flag_picks_that_one(tmp_path: Path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"a": a, "b": b})
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--repo", "b", "-i", "x"])
        assert result.exit_code == 0, result.output
        assert f"cd {b}" in result.output
        assert f"cd {a}" not in result.output
    finally:
        _reset()


def test_dispatch_unknown_repo_errors(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--repo", "nope", "-i", "x"])
        assert result.exit_code == 1
        assert "unknown repo" in result.output
    finally:
        _reset()


def test_dispatch_unknown_task_errors(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "missing", "-i", "x"])
        assert result.exit_code == 1
        assert "Unknown task" in result.output
    finally:
        _reset()


def _override(cfg, state_dir):
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)


def test_dispatch_plan_task_uses_extracted_section(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    plan = tmp_path / "plan.md"
    plan.write_text(
        "<!-- mship:task id=7 -->\n### Task 7\n\nwire the parser\n<!-- /mship:task -->\n"
    )
    _override(cfg, state_dir)
    try:
        result = runner.invoke(
            app, ["dispatch", "--task", "t", "--plan", str(plan), "--plan-task", "7"]
        )
        assert result.exit_code == 0, result.output
        assert "wire the parser" in result.output
    finally:
        _reset()


def test_dispatch_requires_one_instruction_source(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t"])
        assert result.exit_code != 0
        assert "exactly one instruction source" in result.output
    finally:
        _reset()


def test_dispatch_rejects_two_instruction_sources(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    plan = tmp_path / "plan.md"
    plan.write_text("<!-- mship:task id=1 -->\nx\n<!-- /mship:task -->\n")
    _override(cfg, state_dir)
    try:
        result = runner.invoke(
            app,
            ["dispatch", "--task", "t", "-i", "inline", "--plan", str(plan), "--plan-task", "1"],
        )
        assert result.exit_code != 0
        assert "exactly one instruction source" in result.output
    finally:
        _reset()


def test_dispatch_plan_task_without_plan_errors(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "1"])
        assert result.exit_code != 0
        assert "--plan-task requires --plan" in result.output
    finally:
        _reset()


def test_dispatch_instruction_dash_reads_stdin(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "-"], input="from stdin\n")
        assert result.exit_code == 0, result.output
        assert "> from stdin" in result.output
    finally:
        _reset()


def test_dispatch_prompt_includes_dependencies_section(tmp_path: Path):
    now = datetime.now(timezone.utc)
    wt_a = tmp_path / "wt-a"; wt_a.mkdir()
    wt_b = tmp_path / "wt-b"; wt_b.mkdir()
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    StateManager(state_dir).save(WorkspaceState(tasks={
        "a": Task(slug="a", description="a", phase="dev",
                  created_at=now, affected_repos=["mothership"], branch="feat/a",
                  worktrees={"mothership": wt_a}),
        "b": Task(slug="b", description="b", phase="dev",
                  created_at=now, affected_repos=["mothership"], branch="feat/b",
                  worktrees={"mothership": wt_b},
                  depends_on=[DependencyEdge(upstream_slug="a", created_at=now)]),
    }))
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "b", "-i", "go"])
        assert result.exit_code == 0, result.output
        assert "## Dependencies" in result.output
        assert "a" in result.output
        assert "not ready" in result.output
    finally:
        _reset()
