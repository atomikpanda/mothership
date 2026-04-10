from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.state import (
    StateManager,
    Task,
    TestResult,
    WorkspaceState,
)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".mothership"
    d.mkdir()
    return d


def test_empty_state(state_dir: Path):
    mgr = StateManager(state_dir)
    state = mgr.load()
    assert state.current_task is None
    assert state.tasks == {}


def test_save_and_load_roundtrip(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels to tasks",
        phase="plan",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/add-labels",
    )
    state = WorkspaceState(current_task="add-labels", tasks={"add-labels": task})
    mgr.save(state)
    loaded = mgr.load()
    assert loaded.current_task == "add-labels"
    assert loaded.tasks["add-labels"].slug == "add-labels"
    assert loaded.tasks["add-labels"].affected_repos == ["shared", "auth-service"]


def test_save_with_test_results(state_dir: Path):
    mgr = StateManager(state_dir)
    now = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    task = Task(
        slug="fix-auth",
        description="Fix auth",
        phase="dev",
        created_at=now,
        affected_repos=["auth-service"],
        branch="feat/fix-auth",
        test_results={"auth-service": TestResult(status="pass", at=now)},
    )
    state = WorkspaceState(current_task="fix-auth", tasks={"fix-auth": task})
    mgr.save(state)
    loaded = mgr.load()
    assert loaded.tasks["fix-auth"].test_results["auth-service"].status == "pass"


def test_save_with_worktrees(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="add-labels",
        description="Add labels",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
        worktrees={"shared": Path("/tmp/worktree/shared")},
    )
    state = WorkspaceState(current_task="add-labels", tasks={"add-labels": task})
    mgr.save(state)
    loaded = mgr.load()
    assert loaded.tasks["add-labels"].worktrees["shared"] == Path("/tmp/worktree/shared")


def test_creates_state_dir_if_missing(tmp_path: Path):
    state_dir = tmp_path / ".mothership"
    mgr = StateManager(state_dir)
    state = mgr.load()
    assert state.current_task is None
    # Save should create the directory
    mgr.save(state)
    assert state_dir.exists()


def test_get_current_task(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="fix-auth",
        description="Fix auth",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["auth-service"],
        branch="feat/fix-auth",
    )
    state = WorkspaceState(current_task="fix-auth", tasks={"fix-auth": task})
    mgr.save(state)
    current = mgr.get_current_task()
    assert current is not None
    assert current.slug == "fix-auth"


def test_get_current_task_none(state_dir: Path):
    mgr = StateManager(state_dir)
    assert mgr.get_current_task() is None
