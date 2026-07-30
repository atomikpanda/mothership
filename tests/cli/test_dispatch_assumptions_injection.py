"""`mship dispatch --emit` auto-appends the rendered assumption table when
the resolved task is in the plan phase (product-assumptions-wave-2, Task 4:
L2 deterministic injection). Non-plan phases are unaffected."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.assumptions import SEED_ROWS
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


def _bootstrap(tmp_path: Path, phase: str) -> tuple[Path, Path]:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos: {}\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    task = Task(
        slug="t", description="d", phase=phase,
        created_at=datetime.now(timezone.utc),
        affected_repos=["only"], worktrees={"only": wt}, branch="feat/t",
        base_branch="main",
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"t": task}))
    return cfg, state_dir


def _override(cfg, state_dir):
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.log_manager.reset()


def test_emit_for_plan_phase_task_appends_assumption_table(tmp_path: Path):
    cfg, state_dir = _bootstrap(tmp_path, phase="plan")
    _override(cfg, state_dir)
    try:
        result = runner.invoke(
            app, ["dispatch", "--task", "t", "-i", "plan the thing"]
        )
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["dispatch", "--task", "t", "--emit"])
        assert result.exit_code == 0, result.output
        assert "## Assumptions to disposition" in result.output
        for row in SEED_ROWS:
            assert row.axis in result.output
    finally:
        _reset()


def test_emit_for_non_plan_phase_task_omits_assumption_block(tmp_path: Path):
    cfg, state_dir = _bootstrap(tmp_path, phase="dev")
    _override(cfg, state_dir)
    try:
        result = runner.invoke(
            app, ["dispatch", "--task", "t", "-i", "build the thing"]
        )
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["dispatch", "--task", "t", "--emit"])
        assert result.exit_code == 0, result.output
        assert "## Assumptions to disposition" not in result.output
    finally:
        _reset()


def test_emit_plan_phase_encrypted_store_missing_key_errors_cleanly(tmp_path: Path):
    """An encrypted assumptions store with no key (worker without it) must fail
    the plan-phase emit CLEANLY (exit 1 + message), not a raw traceback
    (final-review #1)."""
    from mship.core import spec_key
    from mship.core.assumptions import AssumptionStore, SEED_ROWS
    cfg, state_dir = _bootstrap(tmp_path, phase="plan")
    cfg.write_text("workspace: t\nrepos: {}\nassumption_storage: encrypted\n")
    AssumptionStore(tmp_path, mode="encrypted").save(list(SEED_ROWS))  # generates key
    spec_key.keyfile_path(tmp_path).unlink()  # simulate a worker without the key
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "plan it"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["dispatch", "--task", "t", "--emit"])
        assert result.exit_code == 1
        assert "cannot derive the prompt" in result.output
    finally:
        _reset()


def test_full_dispatch_for_plan_phase_injects_assumptions(tmp_path: Path):
    """`dispatch --full` (the inline prompt path) must ALSO inject assumptions
    for a plan-phase task — not only `--emit` (Greptile #450)."""
    cfg, state_dir = _bootstrap(tmp_path, phase="plan")
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "plan it", "--full"])
        assert result.exit_code == 0, result.output
        assert "## Assumptions to disposition" in result.output
        for row in SEED_ROWS:
            assert row.axis in result.output
    finally:
        _reset()


def test_full_dispatch_for_non_plan_phase_omits_assumptions(tmp_path: Path):
    cfg, state_dir = _bootstrap(tmp_path, phase="dev")
    _override(cfg, state_dir)
    try:
        result = runner.invoke(app, ["dispatch", "--task", "t", "-i", "build it", "--full"])
        assert result.exit_code == 0, result.output
        assert "## Assumptions to disposition" not in result.output
    finally:
        _reset()
