from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from mship.core.remote_tool import ToolContext, ToolRequest
from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.remote_exec import RemoteExecDeps
from mship.core.remote_client import source_update_remote
from mship.core.run_host import RunHostResolver
from mship.core.run_target.models import DiscoveryRequest
from mship.core.session_channel import OwnerContext, _write_receipt, read_private_json
from mship.core.session_inputs import OwnerRequest, SessionError
from mship.core.session_source import (
    SourceUpdateError,
    SourceUpdateRequest,
    SourceUpdateReply,
    SessionSourceUpdateService,
)
from mship.core.tool_process import ToolOperationRegistry
from mship.util.shell import ShellRunner
from tests.core.test_remote_source_snapshot import _git, _host, _repo


def _request() -> SourceUpdateRequest:
    discovery = DiscoveryRequest(
        protocol_version=1,
        backend="mobile",
        backend_revision="a" * 40,
        profile="phone",
        profile_revision="c" * 64,
        task="task-a",
        repo="api",
        operation="reload",
        options={},
        target_alias=None,
    )
    return SourceUpdateRequest(
        operation=ToolRequest(
            task="task-a",
            repo="api",
            argv=(),
            task_key="reload",
            preparation="observe",
            source_revision=discovery.backend_revision,
            owner_ref="o" * 32,
            generation="g" * 32,
            input_files={"MSHIP_TARGET_REQUEST_FILE": discovery.model_dump_json()},
        ),
        run_id="run-a",
        update_id="u" * 32,
        stage="prepare",
        new_source_revision="b" * 40,
        new_profile_revision="d" * 64,
    )


def test_source_update_waits_for_a_slow_phase_without_replaying() -> None:
    request = _request()
    reply = SourceUpdateReply(
        update_id=request.update_id,
        run_id=request.run_id,
        stage="prepared",
        source_revision=request.new_source_revision,
        profile_revision=request.new_profile_revision,
    )
    received: list[object] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            payload = json.dumps(reply.to_dict()).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            # A real socket wait exercises HTTPX's default five-second read limit.
            time.sleep(5.2)
            try:
                self.wfile.write(payload)
            except BrokenPipeError, ConnectionResetError:
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = source_update_remote(
                host=_host(f"http://127.0.0.1:{server.server_port}"),
                resolver=RunHostResolver(),
                request=request,
            )
        finally:
            server.shutdown()
            worker.join(timeout=10)
    assert result == reply
    assert received == [request.to_dict()]


def test_source_update_wire_rejects_caller_environment_authority() -> None:
    request = _request()
    with pytest.raises(SourceUpdateError):
        replace(
            request,
            operation=replace(
                request.operation, env={"MSHIP_TARGET_CONTEXT_FILE": "caller-path"}
            ),
        )


def test_source_update_rejects_ambiguous_target_json() -> None:
    request = _request()
    target = request.operation.input_files["MSHIP_TARGET_REQUEST_FILE"]
    ambiguous = target.replace(
        '"operation":"reload"', '"operation":"run","operation":"reload"'
    )
    with pytest.raises(SourceUpdateError):
        replace(
            request,
            operation=replace(
                request.operation,
                input_files={"MSHIP_TARGET_REQUEST_FILE": ambiguous},
            ),
        )


@pytest.mark.parametrize(
    "changed_path",
    [
        "pubspec.yaml",
        "android/app/src/main/AndroidManifest.xml",
        "runtime-versions",
    ],
)
def test_reload_preflight_rejects_build_inputs_without_mutating_the_live_owner(
    tmp_path, changed_path
):
    repo, original = _repo(tmp_path)
    changed = repo / changed_path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("changed configuration\n")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "configuration",
    )
    new = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "--keep", original)
    _git(repo, "update-ref", "refs/mship/run/task-a/api", new)
    config = WorkspaceConfig(
        workspace="test",
        repos={
            "api": RepoConfig(
                path=repo,
                type="service",
                host_tools={"mise": {"manifest": "runtime-versions"}},
            )
        },
    )
    registry = ToolOperationRegistry(tmp_path)
    cancelled = threading.Event()
    stream = registry.run(
        ToolRequest(
            task="task-a",
            repo="api",
            argv=(sys.executable, "-c", "import time; time.sleep(60)"),
            source_revision=original,
        ),
        ToolContext("task-a", "api", repo, original),
        cancel_event=cancelled,
    )
    started = next(stream).result
    try:
        request = _request()
        discovery = DiscoveryRequest.model_validate_json(
            request.operation.input_files["MSHIP_TARGET_REQUEST_FILE"]
        ).model_copy(update={"backend_revision": original})
        request = replace(
            request,
            new_source_revision=new,
            operation=replace(
                request.operation,
                owner_ref=started.owner_ref,
                generation=started.generation,
                source_revision=original,
                input_files={"MSHIP_TARGET_REQUEST_FILE": discovery.model_dump_json()},
            ),
        )
        service = SessionSourceUpdateService(
            RemoteExecDeps(
                config=config,
                shell=ShellRunner(),
                workspace_root=tmp_path,
                operations=registry,
            )
        )
        with pytest.raises(SourceUpdateError):
            service._validate_source_changes(request)
        assert _git(repo, "rev-parse", "HEAD") == original
        assert (
            registry.status(
                task="task-a",
                repo="api",
                owner_ref=started.owner_ref,
                generation=started.generation,
            ).status
            == "running"
        )
    finally:
        cancelled.set()
        terminal = list(stream)[-1]
        assert terminal.result.status == "cancelled"


def test_reload_completion_requires_owner_signature_and_exact_source_transaction(
    tmp_path,
):
    owner = OwnerContext(
        task="task-a",
        repo="api",
        owner_ref="o" * 32,
        generation="g" * 32,
        source_revision="a" * 40,
        workspace_root=tmp_path,
        worktree=tmp_path,
        private_root=tmp_path,
        socket_path=Path("/tmp/source-receipt-test.sock"),
        secret="s" * 32,
    )
    request = OwnerRequest(
        operation_ref="r" * 32,
        operation="source-release",
        source_revision=owner.source_revision,
        expires_at=time.time() + 30,
    )
    update_id = "u" * 32
    with pytest.raises(SessionError):
        owner.verify_source_reload(request, update_id)
    owner.acknowledge_source_reload(request, update_id)
    owner.verify_source_reload(request, update_id)
    with pytest.raises(SessionError):
        replace(owner, generation="h" * 32).verify_source_reload(request, update_id)
    with pytest.raises(SessionError):
        owner.verify_source_reload(
            replace(request, source_revision="b" * 40), update_id
        )
    with pytest.raises(SessionError):
        owner.verify_source_reload(request, "v" * 32)

    # A helper can fabricate receipt content, but cannot authorize its new claim.
    name = f"source-reload-{request.operation_ref}.json"
    value = read_private_json(tmp_path / name)
    value["receipt"]["update_id"] = "v" * 32
    _write_receipt(tmp_path, name, value)
    with pytest.raises(SessionError):
        owner.verify_source_reload(request, "v" * 32)
