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


# --- bind_files helpers (issue #39) ---

from pathlib import PurePosixPath


def _init_repo_with_ignored_files(tmp_path: Path) -> Path:
    """Git-init a repo with a few tracked and ignored leaf files for bind_files testing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    # Write .gitignore FIRST, before creating ignored files
    (repo / ".gitignore").write_text(
        ".env\n"
        ".env.*\n"
        ".venv/\n"
        "node_modules/\n"
        "apps/*/.env\n"
    )
    # Commit .gitignore so git knows which paths are ignored
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "gitignore"], cwd=repo, check=True, capture_output=True, env=env)

    # Create tracked file
    (repo / "tracked.txt").write_text("tracked\n")
    # Create ignored leaf files (not inside ignored directories)
    (repo / ".env").write_text("ENV=yes\n")
    (repo / ".env.local").write_text("LOCAL=1\n")
    (repo / "apps").mkdir()
    (repo / "apps" / "foo").mkdir()
    (repo / "apps" / "foo" / ".env").write_text("FOO=1\n")
    (repo / "apps" / "bar").mkdir()
    (repo / "apps" / "bar" / ".env").write_text("BAR=1\n")
    # Create empty ignored directories to simulate presence but no enumeration
    # (In a real scenario, these would have many files that we don't want to enumerate)
    (repo / ".venv").mkdir()
    (repo / "node_modules").mkdir()

    # Add only tracked.txt
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "tracked"], cwd=repo, check=True, capture_output=True, env=env)
    return repo


def test_git_ignored_files_lists_ignored_leaf_files(tmp_path: Path):
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager

    repo = _init_repo_with_ignored_files(tmp_path)
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
    )
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "Taskfile.yml"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "taskfile"], cwd=repo, check=True, capture_output=True, env=env)

    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner
    mgr = WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )
    files = mgr._git_ignored_files(repo)
    names = {str(p) for p in files}

    # Leaf ignored files are present
    assert ".env" in names
    assert ".env.local" in names
    assert "apps/foo/.env" in names
    assert "apps/bar/.env" in names

    # Tracked files are NOT present
    assert "tracked.txt" not in names
    assert ".gitignore" not in names

    # Contents of ignored directories are NOT present
    # (Git does not enumerate .venv/*, node_modules/*, etc. when those dirs are gitignored)
    assert not any(n.startswith(".venv/") for n in names), f"should not include .venv contents: {names}"
    assert not any(n.startswith("node_modules/") for n in names), f"should not include node_modules contents: {names}"


def _mgr_stub() -> "WorktreeManager":
    """Minimal WorktreeManager just for calling pure methods; no real deps."""
    from mship.core.worktree import WorktreeManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner
    from mship.core.config import WorkspaceConfig, RepoConfig
    from pathlib import Path
    cfg = WorkspaceConfig(
        workspace="t",
        repos={"r": RepoConfig(path=Path("/tmp/x"), type="service")},
    )
    return WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )


def test_match_bind_patterns_literal_match():
    mgr = _mgr_stub()
    candidates = [PurePosixPath(".env"), PurePosixPath(".env.local")]
    out = mgr._match_bind_patterns([".env"], candidates)
    assert out == [PurePosixPath(".env")]


def test_match_bind_patterns_single_segment_glob():
    mgr = _mgr_stub()
    candidates = [PurePosixPath(".env"), PurePosixPath(".env.local"), PurePosixPath("local.env")]
    out = mgr._match_bind_patterns([".env*"], candidates)
    out_set = {str(p) for p in out}
    assert out_set == {".env", ".env.local"}


def test_match_bind_patterns_question_mark_glob():
    mgr = _mgr_stub()
    candidates = [
        PurePosixPath(".env"),
        PurePosixPath(".env.1"),
        PurePosixPath(".env.10"),
    ]
    out = mgr._match_bind_patterns([".env.?"], candidates)
    out_set = {str(p) for p in out}
    assert out_set == {".env.1"}


def test_match_bind_patterns_double_star_recursive():
    mgr = _mgr_stub()
    candidates = [
        PurePosixPath(".env"),
        PurePosixPath("apps/foo/.env"),
        PurePosixPath("services/bar/.env"),
    ]
    out = mgr._match_bind_patterns(["**/.env"], candidates)
    out_set = {str(p) for p in out}
    assert out_set == {".env", "apps/foo/.env", "services/bar/.env"}


def test_match_bind_patterns_single_level_vs_double_star():
    mgr = _mgr_stub()
    candidates = [
        PurePosixPath("apps/foo/.env"),
        PurePosixPath("apps/foo/bar/.env"),
    ]
    single = mgr._match_bind_patterns(["apps/*/.env"], candidates)
    double = mgr._match_bind_patterns(["apps/**/.env"], candidates)
    assert {str(p) for p in single} == {"apps/foo/.env"}
    assert {str(p) for p in double} == {"apps/foo/.env", "apps/foo/bar/.env"}


def test_match_bind_patterns_multi_pattern_dedup():
    mgr = _mgr_stub()
    candidates = [PurePosixPath(".env"), PurePosixPath(".env.local")]
    out = mgr._match_bind_patterns([".env", ".env*"], candidates)
    out_list = [str(p) for p in out]
    assert out_list.count(".env") == 1
    assert ".env.local" in out_list


def test_match_bind_patterns_empty_patterns():
    mgr = _mgr_stub()
    assert mgr._match_bind_patterns([], [PurePosixPath(".env")]) == []


def test_match_bind_patterns_zero_matches_silent():
    mgr = _mgr_stub()
    assert mgr._match_bind_patterns(["apps/**/.env"], [PurePosixPath(".env")]) == []


import shutil


def test_copy_bind_files_copies_matched_files(tmp_path: Path):
    """End-to-end: given a git repo with ignored files, _copy_bind_files
    copies the listed ones into a fake 'worktree' directory, preserving
    relative paths."""
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner

    repo = _init_repo_with_ignored_files(tmp_path)
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - .env\n"
        "      - apps/**/.env\n"
    )
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    import os, subprocess
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "Taskfile.yml"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "taskfile"], cwd=repo, check=True, capture_output=True, env=env)

    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    mgr = WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )
    worktree = tmp_path / "fake-worktree"
    worktree.mkdir()

    warnings = mgr._copy_bind_files("r", cfg.repos["r"], worktree)
    assert warnings == []

    assert (worktree / ".env").read_text() == "ENV=yes\n"
    assert (worktree / "apps" / "foo" / ".env").read_text() == "FOO=1\n"
    # .env.local NOT copied (pattern was .env and apps/**/.env, not .env.local).
    assert not (worktree / ".env.local").exists()
    # .venv contents NEVER copied.
    assert not (worktree / ".venv").exists()


def test_copy_bind_files_warns_on_missing_literal(tmp_path: Path):
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner

    repo = _init_repo_with_ignored_files(tmp_path)
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - .envv\n"  # typo — does not exist
    )
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    import os, subprocess
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "Taskfile.yml"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "taskfile"], cwd=repo, check=True, capture_output=True, env=env)

    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    mgr = WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )
    worktree = tmp_path / "fake-worktree"
    worktree.mkdir()

    warnings = mgr._copy_bind_files("r", cfg.repos["r"], worktree)
    assert len(warnings) == 1
    assert ".envv" in warnings[0]
    assert "source missing" in warnings[0].lower() or "missing" in warnings[0].lower()


def test_copy_bind_files_zero_glob_matches_silent(tmp_path: Path):
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner

    repo = _init_repo_with_ignored_files(tmp_path)
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - nonexistent/**/.env\n"  # glob matches nothing — silent
    )
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    import os, subprocess
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "Taskfile.yml"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "taskfile"], cwd=repo, check=True, capture_output=True, env=env)

    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    mgr = WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )
    worktree = tmp_path / "fake-worktree"
    worktree.mkdir()

    warnings = mgr._copy_bind_files("r", cfg.repos["r"], worktree)
    assert warnings == []  # No warning: globs that match nothing are silent.


def test_copy_bind_files_preserves_permissions(tmp_path: Path):
    import os, stat, subprocess
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner

    repo = _init_repo_with_ignored_files(tmp_path)
    # Make .env executable (weird but tests permission preservation).
    env_file = repo / ".env"
    env_file.chmod(env_file.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - .env\n"
    )
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    genv = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "Taskfile.yml"], cwd=repo, check=True, capture_output=True, env=genv)
    subprocess.run(["git", "commit", "-qm", "taskfile"], cwd=repo, check=True, capture_output=True, env=genv)

    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    mgr = WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )
    worktree = tmp_path / "fake-worktree"
    worktree.mkdir()
    mgr._copy_bind_files("r", cfg.repos["r"], worktree)

    src_mode = (repo / ".env").stat().st_mode
    dst_mode = (worktree / ".env").stat().st_mode
    assert stat.S_IMODE(src_mode) == stat.S_IMODE(dst_mode)


def test_spawn_copies_bind_files_and_coexists_with_symlink_dirs(tmp_path: Path):
    """Regression: bind_files and symlink_dirs run in the same spawn without interfering."""
    import os, subprocess
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager
    from mship.core.graph import DependencyGraph
    from mship.core.state import StateManager
    from mship.core.log import LogManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner

    # Bare origin + working clone with .gitignore, .env, one tracked file, node_modules/ dir.
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    clone = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (clone / ".gitignore").write_text(".env\nnode_modules/\n")
    (clone / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (clone / ".env").write_text("secret=1\n")
    (clone / "node_modules").mkdir()
    (clone / "node_modules" / "pkg.txt").write_text("pkg\n")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", "init"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True, capture_output=True)

    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    symlink_dirs: [node_modules]\n"
        "    bind_files: [.env]\n"
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    mgr = WorktreeManager(
        config=cfg,
        graph=DependencyGraph(config=cfg),
        state_manager=StateManager(state_dir=state_dir),
        git=GitRunner(),
        shell=ShellRunner(),
        log=LogManager(logs_dir=state_dir / "logs"),
    )
    result = mgr.spawn(description="add labels", skip_setup=True)
    wt = result.task.worktrees["r"]

    # bind_files: .env is copied byte-identical.
    assert (wt / ".env").read_text() == "secret=1\n"
    # symlink_dirs: node_modules is a symlink, not a copy.
    assert (wt / "node_modules").is_symlink()
    # Both succeeded with no warnings.
    bind_warnings = [w for w in result.setup_warnings if "bind_files" in w]
    assert bind_warnings == [], f"unexpected bind_files warnings: {bind_warnings}"
