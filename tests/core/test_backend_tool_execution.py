from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
import pytest

from mship.core import remote_exec
from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.remote_tool import ToolContext, ToolProtocolError, ToolRequest
from mship.core.run_target.host import TARGET_CONTEXT_FILE, TARGET_REQUEST_FILE
from mship.core.run_target.models import (
    BackendConfig,
    HostRequirements,
    RunProfile,
    profile_revision,
)
from mship.core.tool_process import ToolOperationRegistry
from mship.util.shell import ShellRunner

_REVISION = "a" * 40


class _PreparedTask:
    def __init__(self, deps, task, repos, run_ref_repos):
        self.deps = deps
        self.task = task
        self.worktree = deps.workspace_root / "prepared"
        self.worktree.mkdir(exist_ok=True)

    def prepare(self, name):
        return ()

    def context(self, name):
        return ToolContext(
            task=self.task,
            repo=name,
            worktree=self.worktree,
            source_revision=_REVISION,
        )


class _RecordingShell:
    def __init__(self):
        self.calls = []

    def spawn_argv(self, args, cwd, env):
        self.calls.append((tuple(args), dict(env)))
        code = (
            "import json, os\n"
            "from pathlib import Path\n"
            "request = Path(os.environ['MSHIP_TARGET_REQUEST_FILE'])\n"
            "bindings = Path(os.environ['MSHIP_TARGET_BINDINGS_FILE'])\n"
            "print(json.dumps({'request_path': str(request), "
            "'request': json.loads(request.read_text()), "
            "'bindings': json.loads(bindings.read_text()), "
            "'request_mode': request.stat().st_mode & 0o777, "
            "'binding_mode': bindings.stat().st_mode & 0o777}))\n"
        )
        return ShellRunner().spawn_argv((sys.executable, "-c", code), cwd, env)


def _config(tmp_path: Path) -> WorkspaceConfig:
    repo = RepoConfig(
        path=tmp_path,
        type="service",
        tasks={
            "discover": "backend-discover",
            "launch": "backend-launch",
            "setup": "setup",
        },
        run_backends={
            "example": BackendConfig(
                discover_task="discover", operations={"run": "launch"}
            )
        },
        run_profiles={
            "ios": RunProfile(
                backend="example",
                hosts=HostRequirements(roles=("ios",)),
                options={"platform": "ios"},
            )
        },
    )
    return WorkspaceConfig(
        workspace="workspace", run_hosts=["ios"], repos={"app": repo}
    )


def _profile_payload(**updates):
    profile = RunProfile(
        backend="example",
        hosts=HostRequirements(roles=("ios",)),
        options={"platform": "ios"},
    )
    backend = BackendConfig(discover_task="discover", operations={"run": "launch"})
    payload = {
        "protocol_version": 1,
        "backend": "example",
        "backend_revision": _REVISION,
        "profile": "ios",
        "profile_revision": profile_revision(
            profile, backend, prepared_source_revision=_REVISION
        ),
        "task": "task-1",
        "repo": "app",
        "operation": "run",
        "options": {"platform": "ios"},
        "target_alias": None,
    }
    payload.update(updates)
    return json.dumps(payload)


def _discovery_request(*, input_files):
    return ToolRequest(
        task="task-1",
        repo="app",
        argv=(),
        task_key="discover",
        input_files=input_files,
        preparation="discover",
        source_revision=_REVISION,
        max_stdout_bytes=4096,
        max_stderr_bytes=1024,
        timeout_seconds=2,
    )


def test_configured_discovery_materializes_private_profile_files_without_setup(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    caller_selected_path = str(tmp_path / "caller-selected.json")
    request_payload = _profile_payload(target_alias=caller_selected_path)
    shell = _RecordingShell()
    sentinel = tmp_path / "setup-ran"
    monkeypatch.setattr(remote_exec, "_PreparedTask", _PreparedTask)

    def setup_must_not_run(*args, **kwargs):
        sentinel.touch()
        return ()

    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=shell, workspace_root=tmp_path
    )
    events = list(
        remote_exec.run_tool_stream(
            _discovery_request(input_files={TARGET_REQUEST_FILE: request_payload}),
            deps=deps,
        )
    )

    result = events[-1].result
    assert result.status == "completed" and result.exit_code == 0
    received = json.loads(result.stdout)
    assert shell.calls[0][0] == ("task", "backend-discover")
    assert received["request"]["repo"] == "app"
    assert received["bindings"] == {"paths": {}, "aliases": {}}
    assert received["request_mode"] == 0o600
    assert received["binding_mode"] == 0o600
    assert received["request_path"] != caller_selected_path
    assert not Path(received["request_path"]).exists()
    assert not sentinel.exists()


def test_malformed_or_inconsistent_profile_input_fails_before_execution(
    tmp_path, monkeypatch
):
    shell = _RecordingShell()
    monkeypatch.setattr(remote_exec, "_PreparedTask", _PreparedTask)
    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=shell, workspace_root=tmp_path
    )

    malformed = list(
        remote_exec.run_tool_stream(
            _discovery_request(input_files={TARGET_REQUEST_FILE: "not-json"}), deps=deps
        )
    )
    inconsistent = list(
        remote_exec.run_tool_stream(
            _discovery_request(
                input_files={TARGET_REQUEST_FILE: _profile_payload(repo="other")}
            ),
            deps=deps,
        )
    )

    assert malformed[-1].result.status == "invalid"
    assert inconsistent[-1].result.status == "invalid"
    assert shell.calls == []


def test_ambiguous_or_recursive_profile_inputs_fail_closed_before_execution(
    tmp_path, monkeypatch
):
    shell = _RecordingShell()
    monkeypatch.setattr(remote_exec, "_PreparedTask", _PreparedTask)
    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=shell, workspace_root=tmp_path
    )
    payload = _profile_payload()
    duplicate_options = payload.replace(
        '"options": {"platform": "ios"}',
        '"options":{"private-marker":"one","private-marker":"two"}',
    )
    recursive_options = payload.replace(
        '"options": {"platform": "ios"}',
        '"options":' + "[" * 2_000 + "0" + "]" * 2_000,
    )
    duplicate_context = '{"private-marker":"one","private-marker":"two"}'

    results = [
        list(
            remote_exec.run_tool_stream(
                _discovery_request(
                    input_files={TARGET_REQUEST_FILE: duplicate_options}
                ),
                deps=deps,
            )
        ),
        list(
            remote_exec.run_tool_stream(
                _discovery_request(
                    input_files={TARGET_REQUEST_FILE: recursive_options}
                ),
                deps=deps,
            )
        ),
        list(
            remote_exec.run_tool_stream(
                _discovery_request(
                    input_files={
                        TARGET_REQUEST_FILE: payload,
                        TARGET_CONTEXT_FILE: duplicate_context,
                    }
                ),
                deps=deps,
            )
        ),
    ]

    assert all(events[-1].result.status == "invalid" for events in results)
    assert shell.calls == []
    assert "private-marker" not in repr(results)


def test_task_key_rejects_caller_argv_and_unknown_configured_keys(
    tmp_path, monkeypatch
):
    with pytest.raises(ToolProtocolError, match="task key requires empty argv"):
        ToolRequest(
            task="task-1",
            repo="app",
            argv=("caller-selected-command",),
            task_key="discover",
            preparation="discover",
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
            timeout_seconds=2,
        )

    shell = _RecordingShell()
    monkeypatch.setattr(remote_exec, "_PreparedTask", _PreparedTask)
    result = list(
        remote_exec.run_tool_stream(
            ToolRequest(
                task="task-1",
                repo="app",
                argv=(),
                task_key="missing",
                preparation="discover",
                max_stdout_bytes=1024,
                max_stderr_bytes=1024,
                timeout_seconds=2,
            ),
            deps=remote_exec.RemoteExecDeps(
                config=_config(tmp_path), shell=shell, workspace_root=tmp_path
            ),
        )
    )

    assert result[-1].result.status == "invalid"
    assert shell.calls == []


def test_profile_discovery_cannot_select_configured_launch_task(tmp_path, monkeypatch):
    """A discovery request aimed at the configured launch task must not launch."""
    sentinel = tmp_path / "launch-ran"

    class LaunchSentinelShell(_RecordingShell):
        def spawn_argv(self, args, cwd, env):
            if tuple(args) == ("task", "backend-launch"):
                sentinel.touch()
            return super().spawn_argv(args, cwd, env)

    shell = LaunchSentinelShell()
    monkeypatch.setattr(remote_exec, "_PreparedTask", _PreparedTask)
    events = list(
        remote_exec.run_tool_stream(
            ToolRequest(
                task="task-1",
                repo="app",
                argv=(),
                task_key="launch",
                input_files={TARGET_REQUEST_FILE: _profile_payload()},
                preparation="discover",
                source_revision=_REVISION,
                max_stdout_bytes=4096,
                max_stderr_bytes=1024,
                timeout_seconds=2,
            ),
            deps=remote_exec.RemoteExecDeps(
                config=_config(tmp_path), shell=shell, workspace_root=tmp_path
            ),
        )
    )

    assert events[-1].result.status == "invalid"
    assert shell.calls == []
    assert not sentinel.exists()


def test_profile_request_rejects_pinned_source_mismatch_before_execution(
    tmp_path, monkeypatch
):
    shell = _RecordingShell()
    monkeypatch.setattr(remote_exec, "_PreparedTask", _PreparedTask)
    events = list(
        remote_exec.run_tool_stream(
            ToolRequest(
                task="task-1",
                repo="app",
                argv=(),
                task_key="discover",
                input_files={TARGET_REQUEST_FILE: _profile_payload()},
                preparation="discover",
                source_revision="b" * 40,
                max_stdout_bytes=4096,
                max_stderr_bytes=1024,
                timeout_seconds=2,
            ),
            deps=remote_exec.RemoteExecDeps(
                config=_config(tmp_path), shell=shell, workspace_root=tmp_path
            ),
        )
    )

    assert events[-1].result.status == "invalid"
    assert shell.calls == []


def test_input_storage_rejects_symlinked_operation_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    state_dir = tmp_path / "state"
    state_dir.symlink_to(outside, target_is_directory=True)
    registry = ToolOperationRegistry(tmp_path, state_dir=state_dir)
    context = ToolContext(
        task="task", repo="app", worktree=tmp_path, source_revision=_REVISION
    )
    request = ToolRequest(
        task="task",
        repo="app",
        argv=(sys.executable, "-c", "raise SystemExit(0)"),
        input_files={"PRIVATE_FILE": "{}"},
        preparation="discover",
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
        timeout_seconds=2,
    )

    events = list(registry.run(request, context))

    assert events[-1].result.status == "evidence_error"


def test_observer_private_inputs_cannot_replace_parent_inputs(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    context = ToolContext(
        task="task", repo="app", worktree=worktree, source_revision=_REVISION
    )
    registry = ToolOperationRegistry(tmp_path)
    parent = registry._new_operation(
        ToolRequest(
            task="task",
            repo="app",
            argv=(sys.executable, "-c", "raise SystemExit(0)"),
            input_files={"PRIVATE_FILE": "parent"},
            preparation="discover",
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
            timeout_seconds=2,
        ),
        context,
        indexed=True,
    )
    observer = None
    try:
        registry._prepare_inputs(parent)
        observer = registry._new_operation(
            ToolRequest(
                task="task",
                repo="app",
                argv=(sys.executable, "-c", "raise SystemExit(0)"),
                input_files={"PRIVATE_FILE": "observer"},
                preparation="observe",
                owner_ref="owner-abcdef",
                generation="generation-abcdef",
            ),
            context,
            indexed=False,
            parent=parent,
        )
        registry._prepare_inputs(observer)
        parent_file = parent.private_inputs["PRIVATE_FILE"]
        observer_file = observer.private_inputs["PRIVATE_FILE"]
        parent_path = parent.record_path.parent / parent_file.name
        observer_path = observer.record_path.parent / observer_file.name

        assert parent_path != observer_path
        assert parent_path.read_text() == "parent"
        assert observer_path.read_text() == "observer"
        assert stat.S_IMODE(parent_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(observer_path.stat().st_mode) == 0o600
    finally:
        if observer is not None:
            registry._discard_inputs(observer)
            observer.close_context()
        registry._discard_inputs(parent)
        parent.close_context()
