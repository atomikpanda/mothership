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


def test_task_blocked_fields_default_none(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="test",
        description="Test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/test",
    )
    assert task.blocked_reason is None
    assert task.blocked_at is None


def test_task_blocked_roundtrip(state_dir: Path):
    mgr = StateManager(state_dir)
    now = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    task = Task(
        slug="test",
        description="Test",
        phase="dev",
        created_at=now,
        affected_repos=["shared"],
        branch="feat/test",
        blocked_reason="waiting on API key",
        blocked_at=now,
    )
    state = WorkspaceState(current_task="test", tasks={"test": task})
    mgr.save(state)
    loaded = mgr.load()
    assert loaded.tasks["test"].blocked_reason == "waiting on API key"
    assert loaded.tasks["test"].blocked_at == now


def test_task_blocked_cleared(state_dir: Path):
    mgr = StateManager(state_dir)
    now = datetime(2026, 4, 10, 15, 0, 0, tzinfo=timezone.utc)
    task = Task(
        slug="test",
        description="Test",
        phase="dev",
        created_at=now,
        affected_repos=["shared"],
        branch="feat/test",
        blocked_reason="waiting",
        blocked_at=now,
    )
    state = WorkspaceState(current_task="test", tasks={"test": task})
    mgr.save(state)
    loaded = mgr.load()
    loaded.tasks["test"].blocked_reason = None
    loaded.tasks["test"].blocked_at = None
    mgr.save(loaded)
    reloaded = mgr.load()
    assert reloaded.tasks["test"].blocked_reason is None
    assert reloaded.tasks["test"].blocked_at is None


def test_task_pr_urls_default_empty(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="test",
        description="Test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/test",
    )
    assert task.pr_urls == {}


def test_task_pr_urls_roundtrip(state_dir: Path):
    mgr = StateManager(state_dir)
    task = Task(
        slug="test",
        description="Test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/test",
        pr_urls={"shared": "https://github.com/org/shared/pull/18"},
    )
    state = WorkspaceState(current_task="test", tasks={"test": task})
    mgr.save(state)
    loaded = mgr.load()
    assert loaded.tasks["test"].pr_urls["shared"] == "https://github.com/org/shared/pull/18"


def test_task_accepts_active_repo_and_switched_sha_map(tmp_path):
    import yaml
    from datetime import datetime, timezone
    from pathlib import Path

    from mship.core.state import StateManager, Task, WorkspaceState

    sm = StateManager(tmp_path)
    state = WorkspaceState(
        current_task="t",
        tasks={"t": Task(
            slug="t", description="d", phase="dev",
            created_at=datetime.now(timezone.utc),
            affected_repos=["a", "b"], branch="feat/t",
            active_repo="a",
            last_switched_at_sha={"a": {"b": "abc123"}},
        )},
    )
    sm.save(state)

    # Round-trip via the yaml file
    loaded = sm.load()
    task = loaded.tasks["t"]
    assert task.active_repo == "a"
    assert task.last_switched_at_sha == {"a": {"b": "abc123"}}


def test_task_defaults_for_switch_fields():
    from datetime import datetime, timezone
    from mship.core.state import Task

    task = Task(
        slug="t", description="d", phase="plan",
        created_at=datetime.now(timezone.utc),
        affected_repos=["a"], branch="feat/t",
    )
    assert task.active_repo is None
    assert task.last_switched_at_sha == {}


def test_task_defaults_test_iteration_to_zero():
    from datetime import datetime, timezone
    from mship.core.state import Task
    task = Task(
        slug="t", description="d", phase="plan",
        created_at=datetime.now(timezone.utc),
        affected_repos=["a"], branch="feat/t",
    )
    assert task.test_iteration == 0
