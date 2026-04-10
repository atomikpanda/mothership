from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.log import LogManager
from mship.core.phase import PhaseManager, PhaseTransition
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
