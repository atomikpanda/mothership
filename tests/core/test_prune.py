import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.config import ConfigLoader
from mship.core.prune import PruneManager, OrphanedWorktree
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.git import GitRunner


@pytest.fixture
def prune_deps(workspace_with_git: Path):
    workspace = workspace_with_git
    config = ConfigLoader.load(workspace / "mothership.yaml")
    state_dir = workspace / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    return config, state_mgr, git, workspace


def test_scan_no_orphans(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    assert orphans == []


def test_scan_finds_disk_orphan(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    shared_path = workspace / "shared"
    wt_path = shared_path / ".worktrees" / "feat" / "orphan"
    git.worktree_add(repo_path=shared_path, worktree_path=wt_path, branch="feat/orphan")

    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    assert len(orphans) == 1
    assert orphans[0].reason == "not_in_state"
    assert "shared" in orphans[0].repo


def test_scan_finds_state_orphan(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    task = Task(
        slug="ghost",
        description="Ghost task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/ghost",
        worktrees={"shared": Path("/tmp/nonexistent/worktree")},
    )
    state = WorkspaceState(current_task="ghost", tasks={"ghost": task})
    state_mgr.save(state)

    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    assert any(o.reason == "not_on_disk" for o in orphans)


def test_prune_removes_disk_orphan(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    shared_path = workspace / "shared"
    wt_path = shared_path / ".worktrees" / "feat" / "orphan"
    git.worktree_add(repo_path=shared_path, worktree_path=wt_path, branch="feat/orphan")

    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    count = mgr.prune(orphans)
    assert count == 1
    assert not wt_path.exists()


def test_prune_removes_state_orphan(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    task = Task(
        slug="ghost",
        description="Ghost task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/ghost",
        worktrees={"shared": Path("/tmp/nonexistent/worktree")},
    )
    state = WorkspaceState(current_task="ghost", tasks={"ghost": task})
    state_mgr.save(state)

    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    count = mgr.prune(orphans)
    assert count >= 1
    state = state_mgr.load()
    assert "ghost" not in state.tasks
    assert state.current_task is None
