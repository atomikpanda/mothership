from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.config import ConfigLoader, WorkspaceConfig
from mship.core.executor import RepoExecutor, ExecutionResult, RepoResult
from mship.core.graph import DependencyGraph
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellRunner, ShellResult


@pytest.fixture
def mock_shell() -> MagicMock:
    shell = MagicMock(spec=ShellRunner)
    shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    return shell


@pytest.fixture
def executor_deps(workspace: Path, mock_shell: MagicMock):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)

    task = Task(
        slug="test-task",
        description="Test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "auth-service", "api-gateway"],
        branch="feat/test-task",
    )
    state = WorkspaceState(current_task="test-task", tasks={"test-task": task})
    state_mgr.save(state)

    return config, graph, state_mgr, mock_shell


def test_execute_runs_in_dependency_order(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute("test", repos=["shared", "auth-service", "api-gateway"])

    assert mock_shell.run_task.call_count == 3
    cwds = [str(c.kwargs["cwd"]) for c in mock_shell.run_task.call_args_list]
    shared_idx = next(i for i, c in enumerate(cwds) if "shared" in c)
    auth_idx = next(i for i, c in enumerate(cwds) if "auth-service" in c)
    api_idx = next(i for i, c in enumerate(cwds) if "api-gateway" in c)
    assert shared_idx < auth_idx < api_idx


def test_execute_fail_fast(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="fail"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute("test", repos=["shared", "auth-service", "api-gateway"])
    assert result.success is False
    assert mock_shell.run_task.call_count == 1


def test_execute_all_flag(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="fail"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute(
        "test", repos=["shared", "auth-service", "api-gateway"], run_all=True
    )
    assert result.success is False
    assert mock_shell.run_task.call_count == 3
    assert len(result.results) == 3


def test_execute_resolves_task_name_override(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
    tasks:
      test: unit
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute("test", repos=["shared"])
    mock_shell.run_task.assert_called_once()
    call_kwargs = mock_shell.run_task.call_args.kwargs
    assert call_kwargs["actual_task_name"] == "unit"


def test_execute_uses_env_runner(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
env_runner: "dotenvx run --"
repos:
  shared:
    path: ./shared
    type: library
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute("test", repos=["shared"])
    call_kwargs = mock_shell.run_task.call_args.kwargs
    assert call_kwargs["env_runner"] == "dotenvx run --"


def test_execute_repo_override_env_runner(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
env_runner: "dotenvx run --"
repos:
  shared:
    path: ./shared
    type: library
    env_runner: "op run --"
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute("test", repos=["shared"])
    call_kwargs = mock_shell.run_task.call_args.kwargs
    assert call_kwargs["env_runner"] == "op run --"


def test_execute_updates_test_results(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute(
        "test",
        repos=["shared", "auth-service", "api-gateway"],
        task_slug="test-task",
    )
    state = state_mgr.load()
    task = state.tasks["test-task"]
    assert task.test_results["shared"].status == "pass"
    assert task.test_results["auth-service"].status == "pass"
    assert task.test_results["api-gateway"].status == "pass"


def test_upstream_env_no_task_slug(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    env = executor.resolve_upstream_env("auth-service", None)
    assert env == {}


def test_upstream_env_no_worktrees(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    # test-task has no worktrees
    env = executor.resolve_upstream_env("auth-service", "test-task")
    assert env == {}


def test_upstream_env_with_worktrees(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    # Add worktrees to the task
    state = state_mgr.load()
    state.tasks["test-task"].worktrees = {
        "shared": Path("/tmp/shared-wt"),
        "auth-service": Path("/tmp/auth-wt"),
    }
    state_mgr.save(state)

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    env = executor.resolve_upstream_env("auth-service", "test-task")
    assert env["UPSTREAM_SHARED"] == "/tmp/shared-wt"
    assert env["UPSTREAM_SHARED_TYPE"] == "compile"


def test_upstream_env_hyphenated_name(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    state = state_mgr.load()
    state.tasks["test-task"].worktrees = {
        "shared": Path("/tmp/shared-wt"),
        "auth-service": Path("/tmp/auth-wt"),
        "api-gateway": Path("/tmp/api-wt"),
    }
    state_mgr.save(state)

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    env = executor.resolve_upstream_env("api-gateway", "test-task")
    assert "UPSTREAM_SHARED" in env
    assert "UPSTREAM_AUTH_SERVICE" in env


def test_execute_passes_upstream_env(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    state = state_mgr.load()
    state.tasks["test-task"].worktrees = {
        "shared": Path("/tmp/shared-wt"),
        "auth-service": Path("/tmp/auth-wt"),
    }
    state_mgr.save(state)

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute(
        "test",
        repos=["shared", "auth-service"],
        task_slug="test-task",
    )

    # auth-service call should have UPSTREAM_SHARED env
    calls = mock_shell.run_task.call_args_list
    auth_call = next(c for c in calls if "auth-service" in str(c.kwargs["cwd"]))
    assert auth_call.kwargs["env"]["UPSTREAM_SHARED"] == "/tmp/shared-wt"
    assert auth_call.kwargs["env"]["UPSTREAM_SHARED_TYPE"] == "compile"

    # shared call should have no upstream env (no dependencies)
    shared_call = next(c for c in calls if "shared" in str(c.kwargs["cwd"]) and "auth" not in str(c.kwargs["cwd"]))
    assert shared_call.kwargs["env"] is None


def test_execute_uses_worktree_path_when_available(workspace: Path):
    """When a task has worktrees, executor should run in the worktree, not the repo path."""
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    # Create a fake worktree directory
    wt_dir = workspace / "shared" / ".worktrees" / "feat" / "test-wt"
    wt_dir.mkdir(parents=True)

    task = Task(
        slug="wt-test",
        description="Test worktree execution",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/test-wt",
        worktrees={"shared": wt_dir},
    )
    state = WorkspaceState(current_task="wt-test", tasks={"wt-test": task})
    state_mgr.save(state)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute("test", repos=["shared"], task_slug="wt-test")

    # Should run in worktree path, not repo config path
    call_kwargs = mock_shell.run_task.call_args.kwargs
    assert str(call_kwargs["cwd"]) == str(wt_dir)


def test_execute_falls_back_to_repo_path_without_worktree(executor_deps):
    """Without worktrees, executor uses the repo config path."""
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    executor.execute("test", repos=["shared"], task_slug="test-task")

    call_kwargs = mock_shell.run_task.call_args.kwargs
    assert "shared" in str(call_kwargs["cwd"])
    assert ".worktrees" not in str(call_kwargs["cwd"])


def test_upstream_env_includes_type(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text("""\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
  backend:
    path: ./auth-service
    type: service
  ios-app:
    path: ./api-gateway
    type: service
    depends_on:
      - repo: shared
        type: compile
      - repo: backend
        type: runtime
""")
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    task = Task(
        slug="type-test",
        description="Test",
        phase="dev",
        created_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        affected_repos=["shared", "backend", "ios-app"],
        branch="feat/type-test",
        worktrees={
            "shared": Path("/tmp/shared-wt"),
            "backend": Path("/tmp/backend-wt"),
        },
    )
    state = WorkspaceState(current_task="type-test", tasks={"type-test": task})
    state_mgr.save(state)

    mock_shell = MagicMock(spec=ShellRunner)
    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    env = executor.resolve_upstream_env("ios-app", "type-test")
    assert env["UPSTREAM_SHARED"] == "/tmp/shared-wt"
    assert env["UPSTREAM_SHARED_TYPE"] == "compile"
    assert env["UPSTREAM_BACKEND"] == "/tmp/backend-wt"
    assert env["UPSTREAM_BACKEND_TYPE"] == "runtime"


def test_execute_parallel_tiers(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text("""\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
  api-gateway:
    path: ./api-gateway
    type: service
    depends_on: [shared]
""")
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute("test", repos=["shared", "auth-service", "api-gateway"])
    assert result.success
    assert mock_shell.run_task.call_count == 3


def test_cwd_resolves_through_git_root(tmp_path: Path):
    """When git_root is set and no worktree, cwd is parent.path / child.path."""
    root = tmp_path / "monorepo"
    root.mkdir()
    (root / "Taskfile.yml").write_text("version: '3'")
    web = root / "web"
    web.mkdir()
    (web / "Taskfile.yml").write_text("version: '3'")

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
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    cwd = executor._resolve_cwd("web", None)
    assert cwd == web


def test_execute_parallel_failfast_between_tiers(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text("""\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
""")
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=1, stdout="", stderr="fail")

    executor = RepoExecutor(config, graph, state_mgr, mock_shell)
    result = executor.execute("test", repos=["shared", "auth-service"])
    assert not result.success
    assert mock_shell.run_task.call_count == 1
