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
            app, ["dispatch", "--task", "t", "--plan", str(plan), "--plan-task", "7", "--full"]
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


def test_dispatch_plan_task_without_resolvable_plan_errors(tmp_path: Path):
    # --plan-task with no --plan now auto-resolves the task's plan; when there
    # is no linked/discoverable plan at all, it errors with guidance rather than
    # the old "--plan-task requires --plan".
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "1"])
        assert result.exit_code != 0
        assert "no implementation plan" in result.output.lower()
    finally:
        _reset()


def test_dispatch_task_auto_resolves_convention_plan(tmp_path: Path):
    # A plan discoverable at docs/plans/<slug>.md is auto-resolved when --plan
    # is omitted; --plan-task selects the block.
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    plans = tmp_path / "docs" / "plans"; plans.mkdir(parents=True)
    (plans / "t.md").write_text(
        "<!-- mship:task id=1 -->\n### Task 1\n\nfirst thing\n<!-- /mship:task -->\n"
        "<!-- mship:task id=2 -->\n### Task 2\n\nsecond thing\n<!-- /mship:task -->\n"
    )
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "2", "--full"])
        assert result.exit_code == 0, result.output
        assert "Task 2" in result.output
        assert "second thing" in result.output
        assert "first thing" not in result.output
    finally:
        _reset()


def test_dispatch_task_auto_resolves_workitem_linked_plan(tmp_path: Path):
    # A plan linked on the task's WorkItem (WorkItem.plan_path) is auto-resolved
    # even when it lives outside the docs/plans convention.
    from mship.core.workitem_store import WorkItemStore

    now = datetime.now(timezone.utc)
    wt = tmp_path / "wt"; wt.mkdir()
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"; cfg.write_text("workspace: t\nrepos: {}\n")
    explicit = tmp_path / "custom-plan.md"
    explicit.write_text("<!-- mship:task id=3 -->\nlinked plan body\n<!-- /mship:task -->\n")
    items = WorkItemStore(state_dir / "workitems")
    wi = items.create(title="t", kind="feature", workspace="t", now=now)
    items.link_plan(wi.id, "custom-plan.md", now=now)
    task = Task(
        slug="t", description="d", phase="dev", created_at=now,
        affected_repos=["only"], worktrees={"only": wt}, branch="feat/t",
        base_branch="main", work_item_id=wi.id,
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"t": task}))
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "3", "--full"])
        assert result.exit_code == 0, result.output
        assert "linked plan body" in result.output
    finally:
        _reset()


def test_dispatch_explicit_plan_overrides_linked(tmp_path: Path):
    # An explicit --plan still wins over the auto-resolved convention plan.
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    plans = tmp_path / "docs" / "plans"; plans.mkdir(parents=True)
    (plans / "t.md").write_text(
        "<!-- mship:task id=2 -->\nconvention thing\n<!-- /mship:task -->\n"
    )
    explicit = tmp_path / "explicit.md"
    explicit.write_text("<!-- mship:task id=2 -->\nexplicit thing\n<!-- /mship:task -->\n")
    _override(cfg, state_dir)
    try:
        result = runner.invoke(
            app, ["dispatch", "--task", "t", "--plan", str(explicit), "--plan-task", "2", "--full"]
        )
        assert result.exit_code == 0, result.output
        assert "explicit thing" in result.output
        assert "convention thing" not in result.output
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
    task_base_branch: str | None = None,
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
        base_branch=task_base_branch,
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


def test_dispatch_falls_back_to_stored_task_base_before_main(tmp_path: Path):
    """No repo_config.base_branch and no override, but the task recorded a
    non-default base (e.g. the workspace default "staging") -> dispatch honors
    the stored task.base_branch instead of jumping to "main" (Greptile, MOS-229:
    keeps dispatch's fallback consistent with the context path)."""
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap_with_repo_config(
        tmp_path, "only", wt, repo_base_branch=None, task_base_branch="staging",
    )
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "do the thing"])
        assert result.exit_code == 0, result.output
        assert "- **base branch:** staging" in result.output
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


def _write_convention_plan(tmp_path: Path) -> None:
    plans = tmp_path / "docs" / "plans"; plans.mkdir(parents=True)
    (plans / "t.md").write_text(
        "<!-- mship:task id=1 acs=ac2 -->\n### Task 1\n\nfirst thing\n<!-- /mship:task -->\n"
    )


def test_plan_task_dispatch_prints_stub_not_prompt(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _write_convention_plan(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "1"])
        assert result.exit_code == 0, result.output
        assert "record:" in result.output and "model:" in result.output
        assert "Work from (mandatory)" not in result.output
        assert "Your instruction" not in result.output
        assert "first thing" not in result.output
    finally:
        _reset()


def test_plan_task_dispatch_full_flag_prints_prompt(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _write_convention_plan(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(
            app, ["dispatch", "--task", "t", "--plan-task", "1", "--full"]
        )
        assert result.exit_code == 0, result.output
        assert "Work from (mandatory)" in result.output
        assert "first thing" in result.output
    finally:
        _reset()


def test_plan_task_dispatch_persists_record(tmp_path: Path):
    from mship.core.sdd_store import SddStore

    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _write_convention_plan(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "1"])
        assert result.exit_code == 0, result.output
        rec = SddStore(state_dir).find_for_slug("t")
        assert rec is not None
        assert rec.plan_task_id == "1"
        assert rec.acs == ["ac2"]
        assert rec.instruction is None
        assert rec.plan_path is not None and rec.plan_path.endswith("t.md")
    finally:
        _reset()


def test_instruction_dispatch_keeps_full_output_and_persists_record(tmp_path: Path):
    from mship.core.sdd_store import SddStore

    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "do the thing"])
        assert result.exit_code == 0, result.output
        assert "> do the thing" in result.output  # unchanged default output
        rec = SddStore(state_dir).find_for_slug("t")
        assert rec is not None
        assert rec.instruction == "do the thing"
        assert rec.plan_path is None and rec.plan_task_id is None
    finally:
        _reset()


def test_instruction_dispatch_stub_flag_opts_into_stub(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(
            app, ["dispatch", "--task", "t", "-i", "do the thing", "--stub"]
        )
        assert result.exit_code == 0, result.output
        assert "record:" in result.output
        assert "do the thing" not in result.output
    finally:
        _reset()


def test_dispatch_model_flag_recorded(tmp_path: Path):
    from mship.core.sdd_store import SddStore

    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _write_convention_plan(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(
            app, ["dispatch", "--task", "t", "--plan-task", "1", "--model", "haiku"]
        )
        assert result.exit_code == 0, result.output
        assert "model: haiku" in result.output
        rec = SddStore(state_dir).find_for_slug("t")
        assert rec is not None and rec.model == "haiku"
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


def test_emit_after_plan_task_dispatch_prints_prompt(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _write_convention_plan(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "1"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["dispatch", "--task", "t", "--emit"])
        assert result.exit_code == 0, result.output
        assert "first thing" in result.output      # plan body, derived live
        assert "Model:" in result.output
        assert "Work from (mandatory)" in result.output
        # Warnings (here: acs=ac2 with no bound spec) go ONLY to stderr — the
        # stdout prompt must stay cleanly pipeable.
        assert "warning:" in result.stderr
        assert "warning:" not in result.stdout  # .output is the combined stream
    finally:
        _reset()


def test_emit_uses_recorded_resolved_plan_path(tmp_path: Path, monkeypatch):
    # An explicit relative --plan is resolved against the dispatch-time cwd
    # when recorded, so a later --emit (any cwd) reads exactly that file.
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    sub = tmp_path / "sub"; sub.mkdir()
    (sub / "plan.md").write_text(
        "<!-- mship:task id=7 -->\n### Task 7\n\nsubdir plan body\n<!-- /mship:task -->\n"
    )
    _override(cfg, state_dir)
    try:
        monkeypatch.chdir(sub)
        result = runner.invoke(
            app, ["dispatch", "--task", "t", "--plan", "plan.md", "--plan-task", "7"]
        )
        assert result.exit_code == 0, result.output
        monkeypatch.chdir(tmp_path)  # different cwd at emit time
        result = runner.invoke(app, ["dispatch", "--task", "t", "--emit"])
        assert result.exit_code == 0, result.output
        assert "subdir plan body" in result.output
    finally:
        _reset()


def test_emit_without_record_errors(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--emit"])
        assert result.exit_code != 0
        assert "no dispatch record" in result.output
    finally:
        _reset()


def test_emit_rejects_instruction_sources(tmp_path: Path):
    wt = tmp_path / "wt"; wt.mkdir()
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--emit", "--plan-task", "1"])
        assert result.exit_code == 2
        assert "--emit" in result.output
    finally:
        _reset()


# --- reviewer mode (Task 6) ---

def _git_worktree(tmp_path: Path) -> Path:
    """A real git repo usable as the task worktree: base commit + one commit."""
    import subprocess

    wt = tmp_path / "wt"; wt.mkdir()

    def g(*args):
        subprocess.run(["git", *args], cwd=wt, capture_output=True, text=True, check=True)

    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (wt / "f.txt").write_text("base\n")
    g("add", "-A"); g("commit", "-q", "-m", "base")
    g("checkout", "-q", "-B", "main")     # local base branch at the base commit
    g("checkout", "-q", "-b", "feat/t")
    (wt / "f.txt").write_text("changed\n")
    g("commit", "-q", "-am", "work")
    return wt


def test_reviewer_dispatch_prints_stub_and_writes_package(tmp_path: Path):
    wt = _git_worktree(tmp_path)
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _write_convention_plan(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "1"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["dispatch", "--task", "t", "--mode", "reviewer"])
        assert result.exit_code == 0, result.output
        assert "record:" in result.output and "mode: reviewer" in result.output
        review_dir = state_dir / "sdd" / "no-item" / "t" / "review"
        assert (review_dir / "manifest.json").is_file()
        assert list(review_dir.glob("*.diff"))
    finally:
        _reset()


def test_reviewer_emit_prints_paths_and_contract_not_diff(tmp_path: Path):
    wt = _git_worktree(tmp_path)
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _write_convention_plan(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "1"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["dispatch", "--task", "t", "--mode", "reviewer"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["dispatch", "--task", "t", "--emit"])
        assert result.exit_code == 0, result.output
        review_dir = state_dir / "sdd" / "no-item" / "t" / "review"
        assert str(next(review_dir.glob("*.diff"))) in result.stdout
        assert "diff --git" not in result.stdout          # paths, never content
        assert "spec-compliance" in result.stdout.lower()
        assert "quality" in result.stdout.lower()
        assert "READ-ONLY" in result.stdout
    finally:
        _reset()


def test_reviewer_dispatch_without_prior_record_errors(tmp_path: Path):
    wt = _git_worktree(tmp_path)
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--mode", "reviewer"])
        assert result.exit_code != 0
        assert "no dispatch record" in result.output
    finally:
        _reset()


def test_reviewer_dispatch_rejects_instruction_sources(tmp_path: Path):
    wt = _git_worktree(tmp_path)
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _override(cfg, state_dir)
    try:
        result = runner.invoke(
            app, ["dispatch", "--task", "t", "--mode", "reviewer", "--plan-task", "1"]
        )
        assert result.exit_code == 2
        assert "reviewer" in result.output
    finally:
        _reset()


def test_reviewer_dispatch_warns_on_empty_diff(tmp_path: Path):
    """base_sha == HEAD (no commits past base) -> a 0-byte .diff; the
    controller must hear it's dispatching a reviewer at nothing."""
    import subprocess

    wt = tmp_path / "wt"; wt.mkdir()

    def g(*args):
        subprocess.run(["git", *args], cwd=wt, capture_output=True, text=True, check=True)

    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (wt / "f.txt").write_text("base\n")
    g("add", "-A"); g("commit", "-q", "-m", "base")
    g("checkout", "-q", "-B", "main")     # base branch AT HEAD: nothing to diff
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _write_convention_plan(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "1"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["dispatch", "--task", "t", "--mode", "reviewer"])
        assert result.exit_code == 0, result.output
        assert "review package diff is empty" in result.stderr
        assert "reviews nothing" in result.stderr
        assert "review package diff is empty" not in result.stdout  # stub stays pipeable
    finally:
        _reset()


def test_reviewer_emit_with_corrupt_manifest_errors_cleanly(tmp_path: Path):
    wt = _git_worktree(tmp_path)
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _write_convention_plan(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "1"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["dispatch", "--task", "t", "--mode", "reviewer"])
        assert result.exit_code == 0, result.output
        manifest = state_dir / "sdd" / "no-item" / "t" / "review" / "manifest.json"
        manifest.write_text("{not json")
        result = runner.invoke(app, ["dispatch", "--task", "t", "--emit"])
        assert result.exit_code == 1
        assert "manifest is corrupt" in result.output
        assert "Traceback" not in result.output
    finally:
        _reset()


def test_reviewer_record_write_preserves_review_dir(tmp_path: Path):
    """The reviewer record supersedes the implementer record in the SAME keyed
    dir — the review/ package written just before must survive the write."""
    from mship.core.sdd_store import SddStore

    wt = _git_worktree(tmp_path)
    cfg, state_dir = _bootstrap(tmp_path, {"only": wt})
    _write_convention_plan(tmp_path)
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "--plan-task", "1"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["dispatch", "--task", "t", "--mode", "reviewer"])
        assert result.exit_code == 0, result.output
        rec = SddStore(state_dir).find_for_slug("t")
        assert rec is not None and rec.mode == "reviewer"
        assert rec.model == "sonnet"                       # reviewer builtin default
        assert rec.plan_task_id == "1" and rec.acs == ["ac2"]  # pointer fields kept
        review_dir = state_dir / "sdd" / "no-item" / "t" / "review"
        assert (review_dir / "manifest.json").is_file()    # survived the write
    finally:
        _reset()
