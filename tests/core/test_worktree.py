import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.config import ConfigLoader, WorkspaceConfig
from mship.core.graph import DependencyGraph
from mship.core.log import LogManager
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
    log = MagicMock(spec=LogManager)
    return config, graph, state_mgr, git, shell, workspace, log


def test_spawn_creates_worktrees(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("add labels to tasks", repos=["shared", "auth-service"])
    state = state_mgr.load()
    assert "add-labels-to-tasks" in state.tasks
    task = state.tasks["add-labels-to-tasks"]
    assert task.phase == "plan"
    assert set(task.affected_repos) == {"shared", "auth-service"}
    assert task.branch == "feat/add-labels-to-tasks"
    for repo_name in ["shared", "auth-service"]:
        wt_path = task.worktrees[repo_name]
        assert Path(wt_path).exists()


def test_spawn_dependency_order(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("fix auth", repos=["auth-service", "shared"])
    state = state_mgr.load()
    task = state.tasks["fix-auth"]
    assert "shared" in task.worktrees
    assert "auth-service" in task.worktrees


def test_spawn_all_repos(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("big change")
    state = state_mgr.load()
    task = state.tasks["big-change"]
    assert set(task.affected_repos) == {"shared", "auth-service", "api-gateway"}


def test_spawn_ensures_gitignore(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
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

    mgr = WorktreeManager(config, graph, state_mgr, git, shell, MagicMock(spec=LogManager))
    mgr.spawn("custom branch", repos=["shared"])
    state = state_mgr.load()
    task = state.tasks["custom-branch"]
    assert task.branch == "mship/custom-branch"


def test_abort_removes_worktrees(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("to abort", repos=["shared"])
    state = state_mgr.load()
    wt_path = state.tasks["to-abort"].worktrees["shared"]

    mgr.abort("to-abort")
    assert not Path(wt_path).exists()
    state = state_mgr.load()
    assert "to-abort" not in state.tasks


def test_spawn_runs_setup_task(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("with setup", repos=["shared"])
    shell.run_task.assert_called()


def test_spawn_duplicate_slug_raises(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("duplicate test", repos=["shared"])
    with pytest.raises(ValueError, match="already exists"):
        mgr.spawn("duplicate test", repos=["shared"])


def test_abort_succeeds_even_if_branch_delete_fails(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("abort fail test", repos=["shared"])

    # Make branch_delete fail
    original_branch_delete = git.branch_delete
    git.branch_delete = MagicMock(side_effect=Exception("branch delete failed"))

    mgr.abort("abort-fail-test")

    # State should still be cleaned up
    state = state_mgr.load()
    assert "abort-fail-test" not in state.tasks


def test_spawn_skips_git_root_repos(tmp_path: Path):
    """git_root repos don't get their own worktree — they share the parent's."""
    import os
    import subprocess

    # Create a real git repo with a subdirectory
    root = tmp_path / "monorepo"
    root.mkdir()
    (root / "Taskfile.yml").write_text("version: '3'")
    web = root / "web"
    web.mkdir()
    (web / "Taskfile.yml").write_text("version: '3'")
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"}
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, env=git_env)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root, check=True, capture_output=True,
        env=git_env,
    )

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: mono
repos:
  root:
    path: ./monorepo
    type: service
  web:
    path: web
    type: service
    git_root: root
    depends_on: [root]
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    log = MagicMock(spec=LogManager)

    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("mono test", repos=["root", "web"])

    state = state_mgr.load()
    task = state.tasks["mono-test"]

    # root gets a worktree at <root>/.worktrees/feat/mono-test
    assert "root" in task.worktrees
    root_wt = Path(task.worktrees["root"])
    assert root_wt.exists()
    # web's worktree is a subdirectory of root's worktree
    assert "web" in task.worktrees
    web_wt = Path(task.worktrees["web"])
    assert web_wt == root_wt / "web"
    assert web_wt.exists()


def test_spawn_returns_spawn_result_with_task(worktree_deps):
    """spawn now returns SpawnResult, not Task."""
    from mship.core.worktree import SpawnResult
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    result = mgr.spawn("result test", repos=["shared"])
    assert isinstance(result, SpawnResult)
    assert result.task.slug == "result-test"
    assert result.setup_warnings == []


def test_spawn_collects_setup_warnings_on_failure(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    # Make setup return non-zero
    shell.run_task.return_value = ShellResult(
        returncode=1, stdout="", stderr="setup task not found"
    )
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    result = mgr.spawn("warning test", repos=["shared"])
    assert len(result.setup_warnings) == 1
    assert "shared" in result.setup_warnings[0]
    assert "setup" in result.setup_warnings[0].lower()


def test_spawn_skip_setup_does_not_call_setup(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("skip test", repos=["shared"], skip_setup=True)
    # run_task should not have been called (no setup ran)
    shell.run_task.assert_not_called()


def test_create_symlinks_creates_symlink_when_source_exists(tmp_path: Path):
    """When source exists and target doesn't, create the symlink."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Taskfile.yml").write_text("version: '3'")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "some_pkg").mkdir()

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  repo:
    path: ./repo
    type: service
    symlink_dirs: [node_modules]
"""
    )
    from mship.core.config import ConfigLoader
    from mship.core.graph import DependencyGraph
    from mship.core.state import StateManager
    from mship.core.log import LogManager
    from mship.util.git import GitRunner
    from mship.util.shell import ShellRunner

    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    log = MagicMock(spec=LogManager)

    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)

    wt = tmp_path / "wt"
    wt.mkdir()
    warnings = mgr._create_symlinks("repo", config.repos["repo"], wt)

    assert warnings == []
    symlink = wt / "node_modules"
    assert symlink.is_symlink()
    assert symlink.resolve() == (repo / "node_modules").resolve()


def test_create_symlinks_warns_when_source_missing(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Taskfile.yml").write_text("version: '3'")

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  repo:
    path: ./repo
    type: service
    symlink_dirs: [node_modules]
"""
    )
    from mship.core.config import ConfigLoader
    from mship.core.graph import DependencyGraph
    from mship.core.state import StateManager
    from mship.core.log import LogManager
    from mship.util.git import GitRunner
    from mship.util.shell import ShellRunner

    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    log = MagicMock(spec=LogManager)

    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)

    wt = tmp_path / "wt"
    wt.mkdir()
    warnings = mgr._create_symlinks("repo", config.repos["repo"], wt)

    assert len(warnings) == 1
    assert "source missing" in warnings[0]
    assert "node_modules" in warnings[0]
    assert not (wt / "node_modules").exists()


def test_create_symlinks_skips_when_target_is_real_dir(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Taskfile.yml").write_text("version: '3'")
    (repo / "node_modules").mkdir()

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  repo:
    path: ./repo
    type: service
    symlink_dirs: [node_modules]
"""
    )
    from mship.core.config import ConfigLoader
    from mship.core.graph import DependencyGraph
    from mship.core.state import StateManager
    from mship.core.log import LogManager
    from mship.util.git import GitRunner
    from mship.util.shell import ShellRunner

    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    log = MagicMock(spec=LogManager)

    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)

    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "node_modules").mkdir()

    warnings = mgr._create_symlinks("repo", config.repos["repo"], wt)

    assert len(warnings) == 1
    assert "already exists as a real directory" in warnings[0]
    assert not (wt / "node_modules").is_symlink()


def test_create_symlinks_replaces_stale_symlink(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Taskfile.yml").write_text("version: '3'")
    (repo / "node_modules").mkdir()

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  repo:
    path: ./repo
    type: service
    symlink_dirs: [node_modules]
"""
    )
    from mship.core.config import ConfigLoader
    from mship.core.graph import DependencyGraph
    from mship.core.state import StateManager
    from mship.core.log import LogManager
    from mship.util.git import GitRunner
    from mship.util.shell import ShellRunner

    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    log = MagicMock(spec=LogManager)

    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)

    wt = tmp_path / "wt"
    wt.mkdir()
    stale_target = tmp_path / "nonexistent"
    (wt / "node_modules").symlink_to(stale_target)

    warnings = mgr._create_symlinks("repo", config.repos["repo"], wt)

    assert warnings == []
    assert (wt / "node_modules").is_symlink()
    assert (wt / "node_modules").resolve() == (repo / "node_modules").resolve()
