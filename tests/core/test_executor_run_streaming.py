"""Integration tests for `canonical_task == "run"` streaming.

These spin up real subprocesses and assert on captured stdout. They
use shell.run_streaming via the real executor — no mocks on the
subprocess layer.
"""
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.executor import RepoExecutor
from mship.core.graph import DependencyGraph
from mship.util.shell import ShellRunner

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="Integration tests use /bin/sh; skip on Windows.",
)


def _build_executor(
    tmp_path: Path,
    repo_commands: dict[str, str],
    *,
    start_mode: str = "foreground",
    task_key: str = "run",
) -> RepoExecutor:
    """Build a real RepoExecutor over a minimal config. Each repo's
    task_key task is set to an inline shell command — no Taskfile
    indirection. repo_commands maps repo name to the shell command string."""
    repos: dict[str, RepoConfig] = {}
    for name, cmd in repo_commands.items():
        repo_dir = tmp_path / name
        repo_dir.mkdir()
        repos[name] = RepoConfig(
            path=repo_dir,
            type="service",
            tasks={task_key: cmd},
            start_mode=start_mode,
        )
    config = WorkspaceConfig(workspace="t", repos=repos)
    graph = DependencyGraph(config)
    state_mgr = MagicMock()
    state_mgr.load.return_value = MagicMock(tasks={})

    class _Shell(ShellRunner):
        """Override build_command so we don't need `task` CLI installed.
        The test sends raw shell strings through run / run_streaming."""
        def build_command(self, command: str, env_runner: str | None = None) -> str:
            # command is "task <actual-name>" where actual-name is the raw
            # command we stashed in tasks["run"]. Strip the `task ` prefix
            # and execute the command directly.
            if command.startswith("task "):
                return command[len("task "):]
            return command

    return RepoExecutor(
        config=config,
        graph=graph,
        state_manager=state_mgr,
        shell=_Shell(),
        healthcheck=MagicMock(wait=lambda *a, **kw: MagicMock(ready=True, message="")),
    )


def test_foreground_run_streams_stdout_and_stderr(capsys, tmp_path):
    ex = _build_executor(tmp_path, {
        "api": "sh -c 'echo hello; echo world >&2; exit 0'",
    })
    result = ex.execute("run", repos=["api"])
    out = capsys.readouterr().out
    assert "api  | hello" in out
    assert "api  | world" in out
    assert result.success is True


def test_foreground_run_nonzero_returncode_still_streams(capsys, tmp_path):
    ex = _build_executor(tmp_path, {
        "api": "sh -c 'echo oops; exit 2'",
    })
    result = ex.execute("run", repos=["api"])
    out = capsys.readouterr().out
    assert "api  | oops" in out
    assert result.success is False
    assert result.results[0].shell_result.returncode == 2


def test_two_parallel_services_both_visible(capsys, tmp_path):
    ex = _build_executor(tmp_path, {
        "api":    "sh -c 'echo from-api; sleep 0.05; echo api-done'",
        "worker": "sh -c 'echo from-worker; sleep 0.05; echo worker-done'",
    })
    result = ex.execute("run", repos=["api", "worker"])
    out = capsys.readouterr().out
    assert "api     | from-api" in out
    assert "api     | api-done" in out
    assert "worker  | from-worker" in out
    assert "worker  | worker-done" in out
    assert result.success is True


def test_background_run_streams_output(capsys, tmp_path):
    """`start_mode: background` services also get their output relayed."""
    ex = _build_executor(
        tmp_path,
        {"bg": "sh -c 'echo bg-hello; sleep 0.1'"},
        start_mode="background",
    )
    result = ex.execute("run", repos=["bg"])
    # Background subprocess is still alive here; wait for it to finish so
    # drain threads fully relay the output before we assert.
    for proc in result.background_processes:
        proc.wait(timeout=2)
    # Give drain threads a moment to flush.
    import time as _t
    _t.sleep(0.1)
    out = capsys.readouterr().out
    assert "bg  | bg-hello" in out


def test_non_run_task_does_not_stream(capsys, tmp_path):
    """Setup/test/etc stay on the capture path — output should NOT appear
    on our stdout via the printer."""
    ex = _build_executor(
        tmp_path,
        {"r": "echo setup-output"},
        task_key="setup",
    )
    ex.execute("setup", repos=["r"])
    out = capsys.readouterr().out
    # setup-output should NOT appear as a prefixed line — setup uses
    # capture path, returning the string in ShellResult.stdout instead.
    assert "r  | setup-output" not in out
