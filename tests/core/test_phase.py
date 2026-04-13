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
    state = WorkspaceState(current_task="add-labels", tasks={"add-labels": task})
    mgr.save(state)
    return mgr


def test_transition_plan_to_dev(state_with_task: StateManager):
    pm = PhaseManager(state_with_task, MagicMock(spec=LogManager))
    result = pm.transition("add-labels", "dev")
    assert result.new_phase == "dev"
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
        current_task="t",
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
