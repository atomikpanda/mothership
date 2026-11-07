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
    state = WorkspaceState(tasks={"ghost": task})
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
    result = mgr.prune(orphans, writers_stopped=True)
    assert result.count == 1
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
    state = WorkspaceState(tasks={"ghost": task})
    state_mgr.save(state)

    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    result = mgr.prune(orphans, writers_stopped=True)
    assert result.count >= 1
    state = state_mgr.load()
    assert "ghost" not in state.tasks
    assert state.tasks == {}
    from mship.core.retained_worktrees import RetainedWorktreeStore
    assert RetainedWorktreeStore(state_mgr.state_dir).list_records()[0][1] == task


def test_prune_retains_linked_task_metadata_before_last_worktree_removal(prune_deps):
    from mship.core.view.workitem_index import build_workitem_index
    from mship.core.workitem_store import WorkItemStore

    config, state_mgr, git, _workspace = prune_deps
    store = WorkItemStore(state_mgr.state_dir / "workitems")
    item = store.create("pruned", "chore", "ws", datetime.now(timezone.utc))
    task = Task(
        slug="ghost", description="Ghost task", phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"], branch="feat/ghost",
        worktrees={"shared": Path("/tmp/nonexistent/worktree")},
        pr_urls={"shared": "https://github.example/shared/pull/1"},
        work_item_id=item.id,
    )
    store.add_task(item.id, task.slug)
    state_mgr.save(WorkspaceState(tasks={task.slug: task}))

    manager = PruneManager(config, state_mgr, git)
    manager.prune(manager.scan(), writers_stopped=True)

    summary = build_workitem_index([store.get(item.id)], {}, state_mgr.load().tasks, {})[0]
    assert summary.affected_repos == ["shared"]
    assert summary.pr_urls == ["https://github.example/shared/pull/1"]




def test_prune_retains_metadata_for_forward_only_workitem_link(prune_deps):
    from mship.core.workitem_store import WorkItemStore

    config, state_mgr, git, _workspace = prune_deps
    store = WorkItemStore(state_mgr.state_dir / "workitems")
    item = store.create("forward-only", "chore", "ws", datetime.now(timezone.utc))
    task = Task(
        slug="forward-only",
        description="Forward-only task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/forward-only",
        worktrees={"shared": Path("/tmp/nonexistent/forward-only")},
        pr_urls={"shared": "https://github.example/shared/pull/1"},
    )
    store.add_task(item.id, task.slug)
    state_mgr.save(WorkspaceState(tasks={task.slug: task}))

    manager = PruneManager(config, state_mgr, git)
    manager.prune(manager.scan(), writers_stopped=True)

    assert task.slug not in state_mgr.load().tasks
    persisted = store.get(item.id)
    assert persisted.affected_repos == ["shared"]
    assert persisted.pr_urls == ["https://github.example/shared/pull/1"]


def test_prune_refuses_metadata_changed_after_retention(prune_deps, monkeypatch):
    from mship.core.persistence.lifecycle_repository import (
        LifecycleRepository,
    )
    from mship.core.workitem_lifecycle import TaskMetadataRetentionConflictError
    from mship.core.workitem_store import WorkItemStore

    config, state_mgr, git, _workspace = prune_deps
    store = WorkItemStore(state_mgr.state_dir / "workitems")
    item = store.create("pruned", "chore", "ws", datetime.now(timezone.utc))
    task = Task(
        slug="ghost", description="Ghost task", phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"], branch="feat/ghost",
        worktrees={"shared": Path("/tmp/nonexistent/worktree")},
        work_item_id=item.id,
    )
    state_mgr.save(WorkspaceState(tasks={task.slug: task}))
    manager = PruneManager(config, state_mgr, git)

    original = LifecycleRepository.retain_and_prune_task_repos

    def update_then_prune(
        self,
        task_slug,
        repos,
        *,
        now,
        expected_task=None,
    ):
        state_mgr.mutate_task(
            task_slug,
            lambda task: (
                task.affected_repos.append("api-gateway"),
                task.pr_urls.update(
                    {"api-gateway": "https://github.example/api/pull/2"},
                ),
            ),
        )
        return original(
            self,
            task_slug,
            repos,
            now=now,
            expected_task=expected_task,
        )

    monkeypatch.setattr(
        LifecycleRepository,
        "retain_and_prune_task_repos",
        update_then_prune,
    )

    with pytest.raises(TaskMetadataRetentionConflictError, match="Retry"):
        manager.prune(manager.scan(), writers_stopped=True)

    live_task = state_mgr.load().tasks["ghost"]
    assert live_task.affected_repos == ["shared", "api-gateway"]
    assert live_task.pr_urls == {"api-gateway": "https://github.example/api/pull/2"}

def test_prune_partial_missing_keeps_task(prune_deps):
    """If only one worktree is missing, remove the entry but keep the task."""
    config, state_mgr, git, workspace = prune_deps
    # Create a real worktree for auth-service
    auth_path = workspace / "auth-service"
    wt_path = auth_path / ".worktrees" / "feat" / "partial"
    git.worktree_add(repo_path=auth_path, worktree_path=wt_path, branch="feat/partial")

    task = Task(
        slug="partial",
        description="Partial task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service"],
        branch="feat/partial",
        worktrees={
            "shared": Path("/tmp/nonexistent/worktree"),  # missing
            "auth-service": wt_path,  # exists
        },
    )
    state = WorkspaceState(tasks={"partial": task})
    state_mgr.save(state)

    mgr = PruneManager(config, state_mgr, git)
    orphans = mgr.scan()
    result = mgr.prune(orphans, writers_stopped=True)
    assert result.count >= 1

    state = state_mgr.load()
    # Task should still exist (auth-service worktree is still valid)
    assert "partial" in state.tasks
    # But shared worktree entry should be removed
    assert "shared" not in state.tasks["partial"].worktrees
    assert "auth-service" in state.tasks["partial"].worktrees
    from mship.core.retained_worktrees import RetainedWorktreeStore
    assert RetainedWorktreeStore(state_mgr.state_dir).list_records()[0][1] == task


def test_prune_detects_hub_layout_orphan(tmp_path):
    """An orphan dir under <workspace>/.worktrees/<slug>/<repo>/ is detected."""
    import os
    import subprocess
    from mship.core.config import ConfigLoader
    from mship.core.state import StateManager
    from mship.core.prune import PruneManager
    from mship.util.git import GitRunner
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
    )
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "init", "-q", str(tmp_path / "shared")],
                   check=True, capture_output=True)
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "init"],
                   cwd=tmp_path / "shared", check=True, capture_output=True, env=env)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    sm = StateManager(state_dir)
    config = ConfigLoader.load(tmp_path / "mothership.yaml")
    # Seed an orphan: hub-style worktree with no state entry
    orphan = tmp_path / ".worktrees" / "stale-task" / "shared"
    subprocess.run(["git", "worktree", "add", str(orphan), "-b", "feat/stale"],
                   cwd=tmp_path / "shared", check=True, capture_output=True)
    pm = PruneManager(config, sm, GitRunner())
    orphans = pm.scan()
    paths = {str(o.path.resolve()) for o in orphans}
    assert str(orphan.resolve()) in paths


def test_prune_requires_stopped_writers_before_mutation(prune_deps):
    from mship.core.prune import WritersNotStoppedError

    config, state_mgr, git, workspace = prune_deps
    wt_path = workspace / "shared" / ".worktrees" / "feat" / "orphan"
    git.worktree_add(
        repo_path=workspace / "shared",
        worktree_path=wt_path,
        branch="feat/orphan",
    )
    manager = PruneManager(config, state_mgr, git)

    with pytest.raises(WritersNotStoppedError, match="writers_stopped=True"):
        manager.prune(manager.scan())

    assert wt_path.exists()


def test_prune_reports_git_refusal_without_removing_worktree(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    wt_path = workspace / "shared" / ".worktrees" / "feat" / "dirty"
    git.worktree_add(
        repo_path=workspace / "shared",
        worktree_path=wt_path,
        branch="feat/dirty",
    )
    (wt_path / "untracked.txt").write_text("do not remove")
    manager = PruneManager(config, state_mgr, git)

    result = manager.prune(manager.scan(), writers_stopped=True)

    assert result.count == 0
    assert len(result.refusals) == 1
    assert result.refusals[0].path == wt_path
    assert wt_path.exists()


def test_scan_retained_worktree_once_and_preserves_record(prune_deps):
    from mship.core.retained_worktrees import RetainedWorktreeStore

    config, state_mgr, git, workspace = prune_deps
    wt_path = workspace / "shared" / ".worktrees" / "feat" / "retained"
    git.worktree_add(
        repo_path=workspace / "shared",
        worktree_path=wt_path,
        branch="feat/retained",
    )
    retained_task = Task(
        slug="retained",
        description="Retained task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/retained",
        worktrees={"shared": wt_path},
    )
    record = RetainedWorktreeStore(state_mgr.state_dir).retain(retained_task)
    manager = PruneManager(config, state_mgr, git)

    candidates = manager.scan()

    matching = [candidate for candidate in candidates if candidate.path == wt_path]
    assert len(matching) == 1
    assert matching[0].reason == "retained"
    assert matching[0].retained_record == record

    result = manager.prune(candidates, writers_stopped=True)

    assert result.count == 1
    assert not wt_path.exists()
    assert record.exists()
    assert result.recovery_refs


def test_scan_skips_retained_path_still_active(prune_deps):
    from mship.core.retained_worktrees import RetainedWorktreeStore

    config, state_mgr, git, workspace = prune_deps
    wt_path = workspace / "shared" / ".worktrees" / "feat" / "active"
    git.worktree_add(
        repo_path=workspace / "shared",
        worktree_path=wt_path,
        branch="feat/active",
    )
    task = Task(
        slug="active",
        description="Active task",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/active",
        worktrees={"shared": wt_path},
    )
    RetainedWorktreeStore(state_mgr.state_dir).retain(task)
    state_mgr.save(WorkspaceState(tasks={task.slug: task}))

    candidates = PruneManager(config, state_mgr, git).scan()

    assert all(candidate.path != wt_path for candidate in candidates)


def test_prune_never_recursively_deletes_hub_contents(prune_deps):
    config, state_mgr, git, workspace = prune_deps
    wt_path = workspace / ".worktrees" / "stale" / "shared"
    git.worktree_add(
        repo_path=workspace / "shared",
        worktree_path=wt_path,
        branch="feat/stale-hub",
    )
    hub = wt_path.parent
    preserved = hub / "do-not-delete.txt"
    preserved.write_text("preserve me")
    manager = PruneManager(config, state_mgr, git)

    result = manager.prune(manager.scan(), writers_stopped=True)

    assert result.count == 1
    assert not wt_path.exists()
    assert preserved.read_text() == "preserve me"
