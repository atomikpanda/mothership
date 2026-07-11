"""Tests for mship.core.lifecycle_hooks — the `hooks:` runtime dispatcher.

Uses a RecordingShell (real ShellRunner.build_command, faked .run()) so tests
assert exactly what command/cwd/env_runner/timeout a hook resolves to without
touching a real subprocess, plus a couple of true end-to-end tests against the
real ShellRunner to prove the timeout wiring actually works against a live
subprocess. See spec mship-lifecycle-hooks (MOS-220).
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mship.core.config import HookConfig, RepoConfig, WorkspaceConfig
from mship.core.lifecycle_hooks import HookContext, HookRequiredError, run_hooks
from mship.util.shell import ShellResult, ShellRunner


class RecordingShell(ShellRunner):
    """Real build_command (inherited), faked run(): records every call and
    returns/raises whatever the test queues for a matching command substring."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._responses: dict[str, ShellResult] = {}
        self._raises: dict[str, Exception] = {}
        self.default_result = ShellResult(returncode=0, stdout="", stderr="")

    def queue_result(self, command_substr: str, result: ShellResult) -> None:
        self._responses[command_substr] = result

    def queue_raise(self, command_substr: str, exc: Exception) -> None:
        self._raises[command_substr] = exc

    def run(self, command, cwd, env=None, timeout=None):
        self.calls.append(
            {"command": command, "cwd": Path(cwd), "env": env, "timeout": timeout}
        )
        for substr, exc in self._raises.items():
            if substr in command:
                raise exc
        for substr, result in self._responses.items():
            if substr in command:
                return result
        return self.default_result


def _config(repos=None, hooks=None, env_runner=None, hooks_default_timeout=30) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace="test",
        env_runner=env_runner,
        repos=repos or {},
        hooks=hooks or [],
        hooks_default_timeout=hooks_default_timeout,
    )


def _repo(path, env_runner=None) -> RepoConfig:
    return RepoConfig(path=Path(path), type="service", env_runner=env_runner)


# --- matching ---------------------------------------------------------------


def test_no_matching_hooks_returns_empty_and_no_shell_calls():
    shell = RecordingShell()
    config = _config(hooks=[HookConfig(on="task.finished", run="task notify")])
    results = run_hooks("pr.merged", config=config, workspace_root=Path("/ws"), shell=shell)
    assert results == []
    assert shell.calls == []


def test_fires_only_matching_event():
    shell = RecordingShell()
    config = _config(hooks=[
        HookConfig(on="task.finished", run="task notify-finish"),
        HookConfig(on="pr.merged", run="task notify-merge"),
    ])
    results = run_hooks("pr.merged", config=config, workspace_root=Path("/ws"), shell=shell)
    assert len(results) == 1
    assert results[0].ok is True
    assert len(shell.calls) == 1
    assert "notify-merge" in shell.calls[0]["command"]


def test_multiple_hooks_same_event_all_run():
    shell = RecordingShell()
    config = _config(hooks=[
        HookConfig(on="task.finished", run="task a"),
        HookConfig(on="task.finished", run="task b"),
    ])
    results = run_hooks("task.finished", config=config, workspace_root=Path("/ws"), shell=shell)
    assert len(results) == 2
    assert len(shell.calls) == 2


# --- cwd / env_runner resolution ---------------------------------------------


def test_no_repo_uses_workspace_root_and_workspace_env_runner():
    shell = RecordingShell()
    config = _config(env_runner="dotenvx run --", hooks=[
        HookConfig(on="task.finished", run="task notify"),
    ])
    run_hooks("task.finished", config=config, workspace_root=Path("/ws"), shell=shell)
    call = shell.calls[0]
    assert call["cwd"] == Path("/ws")
    assert call["command"] == "dotenvx run -- task notify"


def test_hook_repo_resolves_env_runner_and_cwd():
    shell = RecordingShell()
    config = _config(
        repos={"svc": _repo("/ws/svc", env_runner="direnv exec /ws/svc --")},
        hooks=[HookConfig(on="pr.merged", run="task notify", repo="svc")],
    )
    run_hooks("pr.merged", config=config, workspace_root=Path("/ws"), shell=shell)
    call = shell.calls[0]
    assert call["cwd"] == Path("/ws/svc")
    assert call["command"] == "direnv exec /ws/svc -- task notify"


def test_hook_repo_falls_back_to_workspace_env_runner_when_repo_has_none():
    shell = RecordingShell()
    config = _config(
        env_runner="dotenvx run --",
        repos={"svc": _repo("/ws/svc")},
        hooks=[HookConfig(on="pr.merged", run="task notify", repo="svc")],
    )
    run_hooks("pr.merged", config=config, workspace_root=Path("/ws"), shell=shell)
    assert shell.calls[0]["command"] == "dotenvx run -- task notify"


def test_context_repo_used_when_hook_omits_repo():
    shell = RecordingShell()
    config = _config(
        repos={"svc": _repo("/ws/svc", env_runner="x --")},
        hooks=[HookConfig(on="pr.merged", run="task notify")],
    )
    run_hooks(
        "pr.merged", HookContext(repo="svc"),
        config=config, workspace_root=Path("/ws"), shell=shell,
    )
    assert shell.calls[0]["cwd"] == Path("/ws/svc")


def test_hook_repo_overrides_context_repo():
    shell = RecordingShell()
    config = _config(
        repos={"svc": _repo("/ws/svc"), "other": _repo("/ws/other")},
        hooks=[HookConfig(on="pr.merged", run="task notify", repo="other")],
    )
    run_hooks(
        "pr.merged", HookContext(repo="svc"),
        config=config, workspace_root=Path("/ws"), shell=shell,
    )
    assert shell.calls[0]["cwd"] == Path("/ws/other")


def test_worktree_preferred_over_repo_path_when_task_has_one(tmp_path):
    wt_dir = tmp_path / "wt-svc"
    wt_dir.mkdir()
    shell = RecordingShell()
    config = _config(
        repos={"svc": _repo(tmp_path / "svc")},
        hooks=[HookConfig(on="task.finished", run="task notify", repo="svc")],
    )

    class FakeStateManager:
        def load(self):
            task = SimpleNamespace(worktrees={"svc": wt_dir})
            return SimpleNamespace(tasks={"t": task})

    run_hooks(
        "task.finished", HookContext(task_slug="t"),
        config=config, workspace_root=tmp_path, shell=shell,
        state_manager=FakeStateManager(),
    )
    assert shell.calls[0]["cwd"] == wt_dir


def test_worktree_missing_falls_back_to_repo_path(tmp_path):
    shell = RecordingShell()
    config = _config(
        repos={"svc": _repo(tmp_path / "svc")},
        hooks=[HookConfig(on="task.finished", run="task notify", repo="svc")],
    )

    class FakeStateManager:
        def load(self):
            task = SimpleNamespace(worktrees={"svc": tmp_path / "nonexistent-wt"})
            return SimpleNamespace(tasks={"t": task})

    run_hooks(
        "task.finished", HookContext(task_slug="t"),
        config=config, workspace_root=tmp_path, shell=shell,
        state_manager=FakeStateManager(),
    )
    assert shell.calls[0]["cwd"] == tmp_path / "svc"


# --- fail-open vs required:true ----------------------------------------------


def test_non_required_failure_returns_not_ok_and_does_not_raise():
    shell = RecordingShell()
    shell.queue_result("task notify", ShellResult(returncode=1, stdout="", stderr="boom"))
    config = _config(hooks=[HookConfig(on="task.finished", run="task notify")])
    results = run_hooks("task.finished", config=config, workspace_root=Path("/ws"), shell=shell)
    assert len(results) == 1
    assert results[0].ok is False
    assert "boom" in results[0].error


def test_non_required_failure_logs_warning(caplog):
    caplog.set_level(logging.WARNING, logger="mship.core.lifecycle_hooks")
    shell = RecordingShell()
    shell.queue_result("task notify", ShellResult(returncode=1, stdout="", stderr="boom"))
    config = _config(hooks=[HookConfig(on="task.finished", run="task notify")])
    run_hooks("task.finished", config=config, workspace_root=Path("/ws"), shell=shell)
    assert any("task.finished" in r.message for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_required_failure_raises_hook_required_error():
    # `required: true` is only accepted (at config-load time) on the
    # pre-mutation events — phase.entered.*/workitem.phase.* — so this
    # generic run_hooks()-mechanics test uses one of those rather than
    # task.finished (which now rejects `required: true` at config-load time;
    # see test_config.py's post-hoc-events coverage).
    shell = RecordingShell()
    shell.queue_result("task notify", ShellResult(returncode=1, stdout="", stderr="boom"))
    config = _config(hooks=[HookConfig(on="phase.entered.dev", run="task notify", required=True)])
    with pytest.raises(HookRequiredError):
        run_hooks("phase.entered.dev", config=config, workspace_root=Path("/ws"), shell=shell)


def test_required_failure_stops_subsequent_hooks():
    shell = RecordingShell()
    shell.queue_result("task fails", ShellResult(returncode=1, stdout="", stderr="boom"))
    config = _config(hooks=[
        HookConfig(on="phase.entered.dev", run="task fails", required=True),
        HookConfig(on="phase.entered.dev", run="task never-runs"),
    ])
    with pytest.raises(HookRequiredError):
        run_hooks("phase.entered.dev", config=config, workspace_root=Path("/ws"), shell=shell)
    assert len(shell.calls) == 1


def test_success_does_not_log_warning_or_raise(caplog):
    caplog.set_level(logging.WARNING)
    shell = RecordingShell()
    config = _config(hooks=[HookConfig(on="task.finished", run="task notify")])
    results = run_hooks("task.finished", config=config, workspace_root=Path("/ws"), shell=shell)
    assert results[0].ok is True
    assert caplog.records == []


# --- timeout ------------------------------------------------------------


def test_timeout_enforced_non_required():
    shell = RecordingShell()
    shell.queue_raise("task slow", subprocess.TimeoutExpired(cmd="task slow", timeout=5))
    config = _config(hooks=[HookConfig(on="task.finished", run="task slow", timeout=5)])
    results = run_hooks("task.finished", config=config, workspace_root=Path("/ws"), shell=shell)
    assert results[0].ok is False
    assert results[0].timed_out is True
    assert shell.calls[0]["timeout"] == 5


def test_timeout_uses_hook_specific_value_over_workspace_default():
    shell = RecordingShell()
    config = _config(hooks_default_timeout=30, hooks=[
        HookConfig(on="task.finished", run="task notify", timeout=7),
    ])
    run_hooks("task.finished", config=config, workspace_root=Path("/ws"), shell=shell)
    assert shell.calls[0]["timeout"] == 7


def test_timeout_falls_back_to_workspace_default_when_hook_omits_it():
    shell = RecordingShell()
    config = _config(hooks_default_timeout=45, hooks=[
        HookConfig(on="task.finished", run="task notify"),
    ])
    run_hooks("task.finished", config=config, workspace_root=Path("/ws"), shell=shell)
    assert shell.calls[0]["timeout"] == 45


def test_required_timeout_raises_hook_required_error():
    # See test_required_failure_raises_hook_required_error above — required:
    # true is only config-load-valid on a pre-mutation event.
    shell = RecordingShell()
    shell.queue_raise("task slow", subprocess.TimeoutExpired(cmd="task slow", timeout=1))
    config = _config(hooks=[
        HookConfig(on="phase.entered.dev", run="task slow", required=True, timeout=1),
    ])
    with pytest.raises(HookRequiredError):
        run_hooks("phase.entered.dev", config=config, workspace_root=Path("/ws"), shell=shell)


# --- real ShellRunner end-to-end (no mocking of subprocess) ------------------


def test_real_shell_runner_success_end_to_end(tmp_path):
    marker = tmp_path / "marker.txt"
    config = _config(hooks=[HookConfig(on="task.finished", run=f"touch {marker}")])
    results = run_hooks(
        "task.finished", config=config, workspace_root=tmp_path, shell=ShellRunner(),
    )
    assert results[0].ok is True
    assert marker.exists()


def test_real_shell_runner_timeout_end_to_end(tmp_path):
    config = _config(hooks=[HookConfig(on="task.finished", run="sleep 2", timeout=1)])
    results = run_hooks(
        "task.finished", config=config, workspace_root=tmp_path, shell=ShellRunner(),
    )
    assert results[0].ok is False
    assert results[0].timed_out is True
