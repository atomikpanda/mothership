from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.log import LogManager
from mship.core.phase import FinishedTaskError, PhaseManager, PhaseTransition
from mship.core.state import StateManager, Task, TestResult, WorkspaceState


@pytest.fixture
def state_with_task(tmp_path: Path) -> StateManager:
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/add-labels",
        worktrees={
            "shared": tmp_path / "shared",
            "auth-service": tmp_path / "auth-service",
        },
    )
    state = WorkspaceState(tasks={"add-labels": task})
    mgr.save(state)
    return mgr


def _make_phase_manager(state_mgr: StateManager, workspace_root: Path,
                        spec_paths: list[str] | None = None) -> PhaseManager:
    """Helper: build a PhaseManager with the optional spec_paths override."""
    from mship.core.config import WorkspaceConfig, RepoConfig
    config = WorkspaceConfig(
        workspace="test",
        repos={"shared": RepoConfig(path=Path("./shared"), type="library")},
        spec_paths=spec_paths,
    )
    return PhaseManager(
        state_mgr,
        MagicMock(spec=LogManager),
        config=config,
        workspace_root=workspace_root,
    )


def test_transition_plan_to_dev_warns_when_no_spec_exists(state_with_task: StateManager, tmp_path: Path):
    """No spec at any search path → warn (current behavior, but now actually evidence-based)."""
    pm = _make_phase_manager(state_with_task, tmp_path)
    result = pm.transition("add-labels", "dev")
    assert result.new_phase == "dev"
    assert any("spec" in w.lower() for w in result.warnings)


def test_transition_plan_to_dev_silent_when_spec_at_default_path(
    state_with_task: StateManager, tmp_path: Path,
):
    """Spec at the default `docs/superpowers/specs/` should suppress the warning. See #113."""
    spec_dir = tmp_path / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / "2026-04-27-add-labels-design.md").write_text("# spec\n")
    pm = _make_phase_manager(state_with_task, tmp_path)
    result = pm.transition("add-labels", "dev")
    assert result.new_phase == "dev"
    assert not any("spec" in w.lower() for w in result.warnings), result.warnings


def test_transition_plan_to_dev_silent_when_spec_at_configured_path(
    state_with_task: StateManager, tmp_path: Path,
):
    """A configured `spec_paths` override is honored. See #113."""
    custom = tmp_path / "design" / "specs"
    custom.mkdir(parents=True)
    (custom / "feature.md").write_text("# spec\n")
    pm = _make_phase_manager(state_with_task, tmp_path, spec_paths=["design/specs"])
    result = pm.transition("add-labels", "dev")
    assert not any("spec" in w.lower() for w in result.warnings), result.warnings


def test_transition_plan_to_dev_warns_when_configured_path_empty(
    state_with_task: StateManager, tmp_path: Path,
):
    """Configured spec_paths but no specs → still warns."""
    (tmp_path / "design" / "specs").mkdir(parents=True)
    pm = _make_phase_manager(state_with_task, tmp_path, spec_paths=["design/specs"])
    result = pm.transition("add-labels", "dev")
    assert any("spec" in w.lower() for w in result.warnings)


def test_transition_saves_state(state_with_task: StateManager):
    pm = PhaseManager(state_with_task, MagicMock(spec=LogManager))
    pm.transition("add-labels", "dev")
    reloaded = state_with_task.load()
    assert reloaded.tasks["add-labels"].phase == "dev"


def test_transition_to_plan_no_warnings(state_with_task: StateManager):
    pm = PhaseManager(state_with_task, MagicMock(spec=LogManager))
    result = pm.transition("add-labels", "plan")
    assert result.warnings == []


def test_transition_to_review_warns_no_test_results(state_with_task: StateManager):
    pm = PhaseManager(state_with_task, MagicMock(spec=LogManager))
    pm.transition("add-labels", "dev")
    result = pm.transition("add-labels", "review")
    assert any("test" in w.lower() for w in result.warnings)


def test_transition_to_review_warns_failing_tests(state_with_task: StateManager):
    pm = PhaseManager(state_with_task, MagicMock(spec=LogManager))
    state = state_with_task.load()
    now = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    state.tasks["add-labels"].phase = "dev"
    state.tasks["add-labels"].test_results = {
        "shared": TestResult(status="pass", at=now),
        "auth-service": TestResult(status="fail", at=now),
    }
    state_with_task.save(state)
    result = pm.transition("add-labels", "review")
    assert any("auth-service" in w for w in result.warnings)


def test_transition_to_review_suppresses_warn_when_journal_has_test_state_pass(
    state_with_task: StateManager, tmp_path: Path,
):
    """Journal `test-state=pass` entries count as evidence. See #81."""
    log = LogManager(tmp_path / "logs")
    log.create("add-labels")
    log.append("add-labels", "ran pytest in shared", repo="shared", test_state="pass")
    log.append(
        "add-labels", "ran pytest in auth-service",
        repo="auth-service", test_state="pass",
    )
    state = state_with_task.load()
    state.tasks["add-labels"].phase = "dev"
    state_with_task.save(state)

    pm = PhaseManager(state_with_task, log)
    result = pm.transition("add-labels", "review")
    assert result.warnings == [], (
        f"expected no warnings with journal evidence; got {result.warnings}"
    )


def test_transition_to_review_no_warning_all_pass(state_with_task: StateManager):
    pm = PhaseManager(state_with_task, MagicMock(spec=LogManager))
    state = state_with_task.load()
    now = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    state.tasks["add-labels"].phase = "dev"
    state.tasks["add-labels"].test_results = {
        "shared": TestResult(status="pass", at=now),
        "auth-service": TestResult(status="pass", at=now),
    }
    state_with_task.save(state)
    result = pm.transition("add-labels", "review")
    assert result.warnings == []


def test_backward_transition_allowed(state_with_task: StateManager):
    pm = PhaseManager(state_with_task, MagicMock(spec=LogManager))
    pm.transition("add-labels", "dev")
    result = pm.transition("add-labels", "plan")
    assert result.new_phase == "plan"
    assert result.warnings == []


def test_transition_while_blocked_without_force_keeps_blocked(state_with_task: StateManager):
    """Phase transition without force_unblock should not clear blocked state."""
    state = state_with_task.load()
    from datetime import datetime, timezone
    state.tasks["add-labels"].blocked_reason = "waiting on API key"
    state.tasks["add-labels"].blocked_at = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    state_with_task.save(state)

    pm = PhaseManager(state_with_task, MagicMock(spec=LogManager))
    result = pm.transition("add-labels", "dev")
    assert result.new_phase == "dev"

    # Should still be blocked (no force_unblock)
    reloaded = state_with_task.load()
    assert reloaded.tasks["add-labels"].blocked_reason == "waiting on API key"


def test_transition_while_blocked_with_force_unblocks(state_with_task: StateManager):
    """Phase transition with force_unblock should clear blocked state and warn."""
    state = state_with_task.load()
    from datetime import datetime, timezone
    state.tasks["add-labels"].blocked_reason = "waiting on API key"
    state.tasks["add-labels"].blocked_at = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    state_with_task.save(state)

    pm = PhaseManager(state_with_task, MagicMock(spec=LogManager))
    result = pm.transition("add-labels", "dev", force_unblock=True)
    assert result.new_phase == "dev"
    assert any("blocked" in w.lower() and "waiting on API key" in w for w in result.warnings)

    # Should be unblocked after forced transition
    reloaded = state_with_task.load()
    assert reloaded.tasks["add-labels"].blocked_reason is None
    assert reloaded.tasks["add-labels"].blocked_at is None


# ---------------------------------------------------------------------------
# Task 2: phase_entered_at stamping + finished-task guardrail
# ---------------------------------------------------------------------------

@pytest.fixture
def phase_env(tmp_path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    sm = StateManager(state_dir=state_dir)

    class _FakeLog:
        def __init__(self):
            self.entries: list[tuple[str, str]] = []

        def append(self, slug, msg):
            self.entries.append((slug, msg))

    log = _FakeLog()
    pm = PhaseManager(state_manager=sm, log=log)

    state = WorkspaceState(
        tasks={
            "t": Task(
                slug="t", description="d", phase="plan",
                created_at=datetime.now(timezone.utc),
                affected_repos=["a"], branch="feat/t",
            ),
        },
    )
    sm.save(state)
    return sm, pm, log


def test_transition_stamps_phase_entered_at(phase_env):
    sm, pm, _ = phase_env
    before = datetime.now(timezone.utc)
    pm.transition("t", "dev")
    after = datetime.now(timezone.utc)
    task = sm.load().tasks["t"]
    assert task.phase_entered_at is not None
    assert before <= task.phase_entered_at <= after


def test_transition_on_finished_task_refused(phase_env):
    sm, pm, _ = phase_env
    state = sm.load()
    state.tasks["t"].finished_at = datetime.now(timezone.utc) - timedelta(hours=2)
    sm.save(state)
    with pytest.raises(FinishedTaskError):
        pm.transition("t", "dev")


def test_transition_on_finished_task_allowed_with_force(phase_env):
    sm, pm, _ = phase_env
    state = sm.load()
    state.tasks["t"].finished_at = datetime.now(timezone.utc) - timedelta(hours=2)
    sm.save(state)
    result = pm.transition("t", "dev", force_finished=True)
    assert result.new_phase == "dev"
    # Warning surfaced explaining the override
    assert any("finished" in w.lower() for w in result.warnings)


def test_transition_to_run_on_finished_task_allowed_without_force(phase_env):
    sm, pm, _ = phase_env
    state = sm.load()
    state.tasks["t"].phase = "review"
    state.tasks["t"].finished_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    sm.save(state)
    result = pm.transition("t", "run")
    assert result.new_phase == "run"
