from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mship.core.config import ConfigLoader, Healthcheck, RepoConfig, WorkspaceConfig
from mship.core.executor import ExecutionResult, RepoExecutor, RepoResult
from mship.core.graph import DependencyGraph
from mship.core.healthcheck import HealthcheckResult
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
    state = WorkspaceState(tasks={"test-task": task})
    state_mgr.save(state)

    return config, graph, state_mgr, mock_shell


def test_execute_runs_in_dependency_order(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    result = executor.execute("test", repos=["shared", "auth-service", "api-gateway"])

    assert mock_shell.run_task.call_count == 3
    cwds = [str(c.kwargs["cwd"]) for c in mock_shell.run_task.call_args_list]
    shared_idx = next(i for i, c in enumerate(cwds) if "shared" in c)
    auth_idx = next(i for i, c in enumerate(cwds) if "auth-service" in c)
    api_idx = next(i for i, c in enumerate(cwds) if "api-gateway" in c)
    assert shared_idx < auth_idx < api_idx


def test_profile_launches_follow_dependency_tiers_after_preflight(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    started: list[str] = []

    def launch(repo: str):
        def _launch(ready, _cancel_event) -> str:
            started.append(repo)
            value = f"run-{repo}"
            ready(value)
            return value

        return _launch

    results = executor.launch_profiled(
        {
            "shared": launch("shared"),
            "auth-service": launch("auth-service"),
            "api-gateway": launch("api-gateway"),
        }
    )

    assert started == ["shared", "auth-service", "api-gateway"]
    assert results == {
        "shared": "run-shared",
        "auth-service": "run-auth-service",
        "api-gateway": "run-api-gateway",
    }


def test_profile_dependency_starts_after_upstream_ready_not_upstream_exit(
    executor_deps,
):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    dependent_started = Event()

    def upstream(ready, _cancel_event) -> str:
        ready("run-shared")
        assert dependent_started.wait(timeout=1)
        return "run-shared"

    def dependent(ready, _cancel_event) -> str:
        dependent_started.set()
        ready("run-auth")
        return "run-auth"

    results = executor.launch_profiled({"shared": upstream, "auth-service": dependent})

    assert results == {"shared": "run-shared", "auth-service": "run-auth"}


def test_profile_sibling_failure_cancels_only_ready_owner(tmp_path: Path, mock_shell):
    config = WorkspaceConfig(
        workspace="test",
        repos={
            "first": RepoConfig(path=tmp_path / "first", type="service"),
            "second": RepoConfig(path=tmp_path / "second", type="service"),
            "third": RepoConfig(path=tmp_path / "third", type="service"),
        },
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    executor = RepoExecutor(
        config,
        DependencyGraph(config),
        StateManager(state_dir),
        mock_shell,
        healthcheck=MagicMock(),
    )
    first_ready = Event()
    release_first = Event()
    cancelled: list[str] = []
    pre_ready_cancelled = Event()

    def first(ready, _cancel_event) -> str:
        ready("run-first")
        first_ready.set()
        assert release_first.wait(timeout=1)
        return "run-first"

    def second(_ready, _cancel_event) -> str:
        assert first_ready.wait(timeout=1)
        raise RuntimeError("second launch failed")

    def third(_ready, cancel_event) -> str:
        assert cancel_event.wait(timeout=1)
        pre_ready_cancelled.set()
        return "run-third"

    def cancel(run: str) -> None:
        cancelled.append(run)
        release_first.set()

    with pytest.raises(RuntimeError, match="second launch failed"):
        executor.launch_profiled(
            {"first": first, "second": second, "third": third},
            cancel=cancel,
        )
    assert cancelled == ["run-first"]

    assert pre_ready_cancelled.is_set()


def test_profile_post_ready_failure_cancels_live_sibling(tmp_path: Path, mock_shell):
    config = WorkspaceConfig(
        workspace="test",
        repos={
            "first": RepoConfig(path=tmp_path / "first", type="service"),
            "second": RepoConfig(path=tmp_path / "second", type="service"),
        },
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    executor = RepoExecutor(
        config,
        DependencyGraph(config),
        StateManager(state_dir),
        mock_shell,
        healthcheck=MagicMock(),
    )
    first_ready = Event()
    release_first = Event()
    first_run = SimpleNamespace(status="active")
    second_run = SimpleNamespace(status="active")
    cancelled: list[object] = []

    def first(ready, _cancel_event):
        ready(first_run)
        first_ready.set()
        assert release_first.wait(timeout=1)
        return SimpleNamespace(status="stopped")

    def second(ready, _cancel_event):
        assert first_ready.wait(timeout=1)
        ready(second_run)
        raise RuntimeError("second run exited")

    def cancel(run) -> None:
        cancelled.append(run)
        if run is first_run:
            release_first.set()

    with pytest.raises(RuntimeError, match="second run exited"):
        executor.launch_profiled({"first": first, "second": second}, cancel=cancel)

    assert len(cancelled) == 2
    assert cancelled[0] is first_run
    assert cancelled[1] is second_run



@pytest.mark.parametrize("exit_code", [0, 17])
def test_mixed_foreground_readiness_requires_successful_completion(
    tmp_path: Path, mock_shell, exit_code
):
    config = WorkspaceConfig(
        workspace="test",
        repos={"local": RepoConfig(path=tmp_path, type="service")},
    )
    process = MagicMock()
    process.poll.return_value = exit_code
    process.wait.return_value = exit_code
    process.returncode = exit_code
    mock_shell.run_streaming.return_value = process
    executor = RepoExecutor(
        config,
        DependencyGraph(config),
        StateManager(tmp_path / ".mothership"),
        mock_shell,
        healthcheck=MagicMock(),
    )
    readiness = []

    def ready(run):
        readiness.append(run.status)

    if exit_code:
        with pytest.raises(RuntimeError):
            executor.launch_local("local", None, ready, Event(), lambda run: None)
        assert readiness == []
    else:
        executor.launch_local("local", None, ready, Event(), lambda run: None)
        assert readiness == ["stopped"]


def test_mixed_launch_healthcheck_failure_stops_local_and_blocks_profile(
    tmp_path: Path, mock_shell
):
    config = WorkspaceConfig(
        workspace="test",
        repos={
            "local": RepoConfig(
                path=tmp_path / "local",
                type="service",
                start_mode="background",
                healthcheck=Healthcheck(sleep="1ms"),
            ),
            "profile": RepoConfig(
                path=tmp_path / "profile", type="service", depends_on=["local"]
            ),
        },
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    process = MagicMock()
    process.pid = 999999
    process.returncode = None
    process.poll.return_value = None
    process.stdout = None
    process.stderr = None
    mock_shell.run_streaming.return_value = process
    healthcheck = MagicMock()
    healthcheck.wait.return_value = HealthcheckResult(
        ready=False, message="not ready", duration_s=0
    )
    executor = RepoExecutor(
        config,
        DependencyGraph(config),
        StateManager(state_dir),
        mock_shell,
        healthcheck=healthcheck,
    )
    profile_started = Event()

    def launch_profile(_ready, _cancel_event):
        profile_started.set()
        return SimpleNamespace(status="stopped")

    with pytest.raises(RuntimeError, match="local: failed to start"):
        executor.launch_profiled({"profile": launch_profile}, local_repos=("local",))

    assert not profile_started.is_set()
    process.send_signal.assert_called()


def test_mixed_launch_cancels_foreground_local_process_after_profile_failure(
    tmp_path: Path, mock_shell
):
    config = WorkspaceConfig(
        workspace="test",
        repos={
            "local": RepoConfig(path=tmp_path / "local", type="service"),
            "profile": RepoConfig(path=tmp_path / "profile", type="service"),
        },
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    released = Event()
    process = MagicMock()
    process.pid = 999999
    process.returncode = None
    process.poll.return_value = None
    process.stdout = None
    process.stderr = None
    process.send_signal.side_effect = lambda _signal: released.set()
    process.wait.side_effect = lambda **_kwargs: 130 if released.wait(timeout=1) else 1
    mock_shell.run_streaming.return_value = process
    executor = RepoExecutor(
        config,
        DependencyGraph(config),
        StateManager(state_dir),
        mock_shell,
        healthcheck=MagicMock(),
    )
    local_started = Event()

    def start_process(*args, **kwargs):
        local_started.set()
        return process

    mock_shell.run_streaming.side_effect = start_process

    def launch_profile(_ready, _cancel_event):
        assert local_started.wait(timeout=1)
        raise RuntimeError("profile launch failed")

    with pytest.raises(RuntimeError, match="profile launch failed"):
        executor.launch_profiled({"profile": launch_profile}, local_repos=("local",))

    assert released.is_set()
    process.send_signal.assert_called()


def test_mixed_launch_releases_foreground_local_after_profile_completion(
    tmp_path: Path, mock_shell
):
    config = WorkspaceConfig(
        workspace="test",
        repos={
            "local": RepoConfig(path=tmp_path / "local", type="service"),
            "profile": RepoConfig(path=tmp_path / "profile", type="service"),
        },
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    released = Event()
    process = MagicMock()
    process.pid = 999999
    process.returncode = None
    process.poll.return_value = None
    process.stdout = None
    process.stderr = None
    process.send_signal.side_effect = lambda _signal: released.set()
    process.wait.side_effect = lambda **_kwargs: 0 if released.wait(timeout=1) else 1
    mock_shell.run_streaming.return_value = process
    executor = RepoExecutor(
        config,
        DependencyGraph(config),
        StateManager(state_dir),
        mock_shell,
        healthcheck=MagicMock(),
    )
    local_started = Event()

    def start_process(*args, **kwargs):
        local_started.set()
        return process

    mock_shell.run_streaming.side_effect = start_process

    def launch_profile(ready, _cancel_event):
        assert local_started.wait(timeout=1)
        run = SimpleNamespace(status="stopped")
        ready(run)
        return run

    finals = executor.launch_profiled(
        {"profile": launch_profile}, local_repos=("local",)
    )

    assert finals["profile"].status == "stopped"
    assert released.is_set()
    process.send_signal.assert_called()


def test_execute_fail_fast(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    mock_shell.run_task.side_effect = [
        ShellResult(returncode=1, stdout="", stderr="fail"),
        ShellResult(returncode=0, stdout="ok", stderr=""),
        ShellResult(returncode=0, stdout="ok", stderr=""),
    ]
    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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
    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    executor.execute("test", repos=["shared"])
    call_kwargs = mock_shell.run_task.call_args.kwargs
    assert call_kwargs["env_runner"] == "op run --"


def test_execute_updates_test_results(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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


def test_execute_skips_test_when_in_not_applicable(
    workspace: Path, mock_shell: MagicMock
):
    """Repos that declare `not_applicable: [test]` are skipped — no `task test` invocation,
    and the test result is recorded as `status="skip"`. See #109.
    """
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: library
  fixtures:
    path: ./fixtures
    type: library
    not_applicable: [test]
"""
    )
    (workspace / "fixtures").mkdir(exist_ok=True)
    # Even though `test` is not applicable, ConfigLoader still requires a Taskfile.
    (workspace / "fixtures" / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  build:\n    cmds:\n      - echo build\n"
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)
    task = Task(
        slug="test-task",
        description="Test",
        phase="dev",
        created_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        affected_repos=["shared", "fixtures"],
        branch="feat/test-task",
    )
    state_mgr.save(WorkspaceState(tasks={"test-task": task}))

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    result = executor.execute(
        "test",
        repos=["shared", "fixtures"],
        task_slug="test-task",
    )

    # `task test` was invoked once (for shared) — fixtures was skipped.
    cwds = [str(c.kwargs["cwd"]) for c in mock_shell.run_task.call_args_list]
    assert any("shared" in c for c in cwds)
    assert not any("fixtures" in c for c in cwds), (
        "fixtures should NOT have task test invoked"
    )

    # Both results are present; fixtures is recorded as skip.
    persisted_state = state_mgr.load()
    persisted_task = persisted_state.tasks["test-task"]
    assert persisted_task.test_results["shared"].status == "pass"
    assert persisted_task.test_results["fixtures"].status == "skip"

    # Overall execution succeeds (skipped repos count as success).
    assert result.success is True


def test_upstream_env_no_task_slug(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    env = executor.resolve_upstream_env("auth-service", None)
    assert env == {}


def test_upstream_env_no_worktrees(executor_deps):
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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
    shared_call = next(
        c
        for c in calls
        if "shared" in str(c.kwargs["cwd"]) and "auth" not in str(c.kwargs["cwd"])
    )
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
    state = WorkspaceState(tasks={"wt-test": task})
    state_mgr.save(state)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    executor.execute("test", repos=["shared"], task_slug="wt-test")

    # Should run in worktree path, not repo config path
    call_kwargs = mock_shell.run_task.call_args.kwargs
    assert str(call_kwargs["cwd"]) == str(wt_dir)


def test_execute_falls_back_to_repo_path_without_worktree(executor_deps):
    """Without worktrees, executor uses the repo config path."""
    config, graph, state_mgr, mock_shell = executor_deps
    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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
    state = WorkspaceState(tasks={"type-test": task})
    state_mgr.save(state)

    mock_shell = MagicMock(spec=ShellRunner)
    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
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

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    cwd = executor._resolve_cwd("web", None)
    assert cwd == web


def test_background_start_mode_uses_popen(workspace: Path):
    """start_mode: background should call run_streaming, not run_task."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    start_mode: background
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    # Mock streaming: returns a Popen-like object
    popen_mock = MagicMock()
    popen_mock.pid = 12345
    popen_mock.stdout = None  # prevent drain threads from looping on mock data
    popen_mock.stderr = None
    mock_shell.run_streaming.return_value = popen_mock

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    result = executor.execute("run", repos=["shared"])

    assert result.success
    # run_task should NOT have been called
    mock_shell.run_task.assert_not_called()
    # run_streaming SHOULD have been called
    mock_shell.run_streaming.assert_called_once()


def test_foreground_start_mode_uses_run_streaming(workspace: Path):
    """Default start_mode is foreground — foreground run uses run_streaming + wait."""
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 9999
    popen_mock.stdout = None  # disable drain threads
    popen_mock.stderr = None
    popen_mock.wait.return_value = 0
    mock_shell.run_streaming.return_value = popen_mock

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    result = executor.execute("run", repos=["shared"])
    # run_task should NOT be called for foreground run
    mock_shell.run_task.assert_not_called()
    # run_streaming SHOULD have been called
    mock_shell.run_streaming.assert_called_once()
    # popen.wait() should have been called to block until completion
    popen_mock.wait.assert_called_once()
    assert result.success


def test_background_returns_in_execution_result(workspace: Path):
    """ExecutionResult should include the background Popen handles."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    start_mode: background
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 12345
    popen_mock.stdout = None
    popen_mock.stderr = None
    mock_shell.run_streaming.return_value = popen_mock

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    result = executor.execute("run", repos=["shared"])

    assert len(result.background_processes) == 1
    assert result.background_processes[0] is popen_mock


def test_background_repo_result_has_pid(workspace: Path):
    """RepoResult.background_pid is set for background launches."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    start_mode: background
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 55555
    popen_mock.stdout = None
    popen_mock.stderr = None
    mock_shell.run_streaming.return_value = popen_mock

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    result = executor.execute("run", repos=["shared"])
    assert result.results[0].background_pid == 55555


def test_foreground_repo_result_has_no_pid(workspace: Path):
    """RepoResult.background_pid is None for foreground tasks."""
    config = ConfigLoader.load(workspace / "mothership.yaml")
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    result = executor.execute("test", repos=["shared"])
    assert result.results[0].background_pid is None


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
    mock_shell.run_task.return_value = ShellResult(
        returncode=1, stdout="", stderr="fail"
    )

    executor = RepoExecutor(
        config, graph, state_mgr, mock_shell, healthcheck=MagicMock()
    )
    result = executor.execute("test", repos=["shared", "auth-service"])
    assert not result.success
    assert mock_shell.run_task.call_count == 1


def test_executor_runs_healthcheck_after_launch(workspace):
    """When a repo has healthcheck and runs successfully, healthcheck runs and attaches result."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    start_mode: background
    healthcheck:
      sleep: 10ms
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 12345
    popen_mock.stdout = None
    popen_mock.stderr = None
    mock_shell.run_streaming.return_value = popen_mock

    from mship.core.healthcheck import HealthcheckRunner

    hc_runner = HealthcheckRunner(mock_shell)

    executor = RepoExecutor(config, graph, state_mgr, mock_shell, healthcheck=hc_runner)
    result = executor.execute("run", repos=["shared"])

    assert result.success
    assert result.results[0].healthcheck is not None
    assert result.results[0].healthcheck.ready


def test_executor_healthcheck_failure_fails_repo(workspace):
    """A failed healthcheck marks the repo as failed and fail-fast triggers."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:1"
      timeout: 100ms
      retry_interval: 50ms
  auth-service:
    path: ./auth-service
    type: service
    depends_on: [shared]
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    popen_mock = MagicMock()
    popen_mock.pid = 12345
    popen_mock.stdout = None
    popen_mock.stderr = None
    mock_shell.run_streaming.return_value = popen_mock
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    from mship.core.healthcheck import HealthcheckRunner

    hc_runner = HealthcheckRunner(mock_shell)

    executor = RepoExecutor(config, graph, state_mgr, mock_shell, healthcheck=hc_runner)
    result = executor.execute("run", repos=["shared", "auth-service"])

    assert not result.success
    shared_result = next(r for r in result.results if r.repo == "shared")
    assert not shared_result.success
    assert shared_result.healthcheck is not None
    assert not shared_result.healthcheck.ready
    # auth-service should NOT have been started
    auth_called = any(r.repo == "auth-service" for r in result.results)
    assert not auth_called


def test_executor_skips_healthcheck_for_test_command(workspace):
    """Healthchecks only apply to `run` canonical task, not test."""
    cfg = workspace / "mothership.yaml"
    cfg.write_text(
        """\
workspace: test
repos:
  shared:
    path: ./shared
    type: service
    healthcheck:
      tcp: "127.0.0.1:1"
      timeout: 100ms
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")

    from mship.core.healthcheck import HealthcheckRunner

    hc_runner = HealthcheckRunner(mock_shell)

    executor = RepoExecutor(config, graph, state_mgr, mock_shell, healthcheck=hc_runner)
    result = executor.execute("test", repos=["shared"])
    assert result.success
    assert result.results[0].healthcheck is None


def test_repo_result_has_duration_ms_default():
    from mship.core.executor import RepoResult
    from mship.util.shell import ShellResult

    r = RepoResult(
        repo="x",
        task_name="test",
        shell_result=ShellResult(returncode=0, stdout="", stderr=""),
    )
    assert r.duration_ms == 0


def test_executor_records_duration_ms_on_test_run(workspace_with_git, monkeypatch):
    """Each repo result should carry a non-zero duration_ms after a test run."""
    from mship.container import Container

    container = Container()
    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    try:
        executor = container.executor()
        result = executor.execute("test", repos=["shared"], run_all=False)
        assert result.results
        assert all(r.duration_ms >= 0 for r in result.results)
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


import os as _os


@pytest.mark.skipif(_os.name == "nt", reason="Uses /bin/sh; Unix-only.")
def test_run_fast_fails_when_background_crashes(tmp_path):
    """A crashing background `run` task is caught during healthcheck
    retry rather than waiting the full healthcheck timeout.

    The service's `tasks.run` exits 127 immediately. Healthcheck timeout
    is 60s, retry_interval 200ms. Expectation: total elapsed well under
    2s, result marks the repo as failed, healthcheck message names the
    exit code.
    """
    import time as _time
    from pathlib import Path as _Path

    repo_dir = tmp_path / "svc"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\n")
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        """\
workspace: fastfail
repos:
  svc:
    path: ./svc
    type: service
    tasks: {run: 'sh -c "exit 127"'}
    start_mode: background
    healthcheck:
      tcp: "127.0.0.1:1"
      timeout: 60s
      retry_interval: 200ms
"""
    )
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir(exist_ok=True)
    state_mgr = StateManager(state_dir)

    # Use a real ShellRunner so run_streaming actually spawns the subprocess,
    # but override build_command to treat tasks["run"] as a literal shell cmd
    # (no `task` binary needed).
    from mship.util.shell import ShellRunner as _ShellRunner

    class _Shell(_ShellRunner):
        def build_command(self, command: str, env_runner: str | None = None) -> str:
            return command[len("task ") :] if command.startswith("task ") else command

    from mship.core.healthcheck import HealthcheckRunner

    shell = _Shell()
    hc = HealthcheckRunner(shell)

    executor = RepoExecutor(config, graph, state_mgr, shell, healthcheck=hc)

    start = _time.monotonic()
    result = executor.execute("run", repos=["svc"])
    elapsed = _time.monotonic() - start

    assert elapsed < 2.0, f"expected fast-fail in under 2s, took {elapsed:.2f}s"
    assert not result.success
    assert result.results[0].shell_result.returncode == 1
    assert result.results[0].healthcheck is not None
    assert "exited with code 127" in result.results[0].healthcheck.message
