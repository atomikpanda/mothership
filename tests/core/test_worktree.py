import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.config import ConfigLoader, WorkspaceConfig
from mship.core.graph import DependencyGraph
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.worktree import WorktreeManager
from mship.util.git import GitRunner
from mship.util.shell import ShellRunner, ShellResult
from mship.util.slug import slugify


@pytest.fixture
def worktree_deps(workspace_with_git: Path):
    workspace = workspace_with_git
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    return config, graph, state_mgr, git, shell, workspace


def test_spawn_creates_worktrees(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("add labels to tasks", repos=["shared", "auth-service"])
    state = state_mgr.load()
    assert state.current_task == "add-labels-to-tasks"
    task = state.tasks["add-labels-to-tasks"]
    assert task.phase == "plan"
    assert set(task.affected_repos) == {"shared", "auth-service"}
    assert task.branch == "feat/add-labels-to-tasks"
    for repo_name in ["shared", "auth-service"]:
        wt_path = task.worktrees[repo_name]
        assert Path(wt_path).exists()


def test_spawn_dependency_order(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("fix auth", repos=["auth-service", "shared"])
    state = state_mgr.load()
    task = state.tasks["fix-auth"]
    assert "shared" in task.worktrees
    assert "auth-service" in task.worktrees


def test_spawn_all_repos(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("big change")
    state = state_mgr.load()
    task = state.tasks["big-change"]
    assert set(task.affected_repos) == {"shared", "auth-service", "api-gateway"}


def test_spawn_ensures_gitignore(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("test gitignore", repos=["shared"])
    gitignore = workspace / "shared" / ".gitignore"
    assert gitignore.exists()
    assert ".worktrees" in gitignore.read_text()


def test_spawn_custom_branch_pattern(workspace_with_git: Path):
    workspace = workspace_with_git
    cfg = workspace / "mothership.yaml"
    content = cfg.read_text()
    cfg.write_text(content + 'branch_pattern: "mship/{slug}"\n')
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("custom branch", repos=["shared"])
    state = state_mgr.load()
    task = state.tasks["custom-branch"]
    assert task.branch == "mship/custom-branch"


def test_abort_removes_worktrees(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("to abort", repos=["shared"])
    state = state_mgr.load()
    wt_path = state.tasks["to-abort"].worktrees["shared"]

    mgr.abort("to-abort")
    assert not Path(wt_path).exists()
    state = state_mgr.load()
    assert "to-abort" not in state.tasks
    assert state.current_task is None


def test_spawn_runs_setup_task(worktree_deps):
    config, graph, state_mgr, git, shell, workspace = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell)
    mgr.spawn("with setup", repos=["shared"])
    shell.run_task.assert_called()
