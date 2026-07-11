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


def test_dispatch_plan_without_plan_task_errors(tmp_path: Path):
    # --plan is only meaningful with --plan-task; reject it rather than
    # silently discarding the plan when paired with an inline instruction.
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    plan = tmp_path / "plan.md"
    plan.write_text("<!-- mship:task id=1 -->\nx\n<!-- /mship:task -->\n")
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "inline", "--plan", str(plan)])
        assert result.exit_code != 0
        assert "--plan requires --plan-task" in result.output
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


def test_dispatch_default_mode_reports_back_no_pr(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "do the thing"])
        assert result.exit_code == 0, result.output
        assert "Report back" in result.output
        assert "status report" in result.output.lower()
        assert "How to finish" not in result.output
        assert "mship finish --body-file" not in result.output
    finally:
        _reset()


def test_dispatch_standalone_mode_has_finish_contract(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--mode", "standalone", "-i", "x"])
        assert result.exit_code == 0, result.output
        assert "How to finish" in result.output
        assert "mship finish --body-file" in result.output
    finally:
        _reset()


def test_dispatch_invalid_mode_errors(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--mode", "bogus", "-i", "x"])
        assert result.exit_code == 2
        assert "implementer" in result.output
        assert "standalone" in result.output
    finally:
        _reset()


def _bootstrap_with_repo_config(
    tmp_path: Path,
    repo_name: str,
    worktree: Path,
    *,
    repo_base_branch: str | None,
    base_override: str | None = None,
) -> tuple[Path, Path]:
    """Bootstrap a task plus a mothership.yaml that actually declares `repo_name`
    (with an optional `base_branch:`), so dispatch can exercise resolve_base
    against real repo config instead of the empty `repos: {}` used elsewhere
    in this file."""
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    repo_dir = tmp_path / f"{repo_name}-main"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    base_line = f"    base_branch: {repo_base_branch}\n" if repo_base_branch else ""
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        f"  {repo_name}:\n"
        f"    path: {repo_dir}\n"
        "    type: library\n"
        f"{base_line}"
    )
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=[repo_name],
        worktrees={repo_name: worktree}, branch="feat/t",
        active_repo=repo_name, base_override=base_override,
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"t": task}))
    return cfg, state_dir


def test_dispatch_uses_repo_config_base_branch(tmp_path: Path):
    """repo_config.base_branch="dev" (no override) -> the prompt shows "dev",
    not "main" (MOS-229: dispatch used to ignore repo config entirely)."""
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap_with_repo_config(
        tmp_path, "only", wt, repo_base_branch="dev",
    )
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "do the thing"])
        assert result.exit_code == 0, result.output
        assert "- **base branch:** dev" in result.output
        assert "base (dev)" in result.output
    finally:
        _reset()


def test_dispatch_base_override_wins_over_repo_config(tmp_path: Path):
    """task.base_override (the --base pin) takes precedence over repo_config.base_branch."""
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap_with_repo_config(
        tmp_path, "only", wt, repo_base_branch="dev", base_override="stacked",
    )
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "do the thing"])
        assert result.exit_code == 0, result.output
        assert "- **base branch:** stacked" in result.output
    finally:
        _reset()


def test_dispatch_falls_back_to_main_when_repo_config_has_no_base_branch(tmp_path: Path):
    """No repo_config.base_branch and no override -> unchanged "main" fallback."""
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap_with_repo_config(
        tmp_path, "only", wt, repo_base_branch=None,
    )
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "do the thing"])
        assert result.exit_code == 0, result.output
        assert "- **base branch:** main" in result.output
    finally:
        _reset()


def test_dispatch_repo_missing_from_config_falls_back_to_main(tmp_path: Path):
    """A repo not declared in mothership.yaml at all (empty `repos: {}`, as in
    the other tests in this file) must not crash — resolve_base tolerates a
    missing repo_config and dispatch falls back to "main"."""
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "x"])
        assert result.exit_code == 0, result.output
        assert "- **base branch:** main" in result.output
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
