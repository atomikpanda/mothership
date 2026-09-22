from __future__ import annotations

import os
import subprocess
import io
import json
import sys
import tarfile
from dataclasses import replace

import pytest

from mship.backends import common

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.remote_exec import RemoteExecDeps, run_observe_capture_stream
from mship.core.remote_tool import ToolContext, ToolRequest
from mship.core.run_target.models import DiscoveryRequest, profile_revision
from mship.core.tool_process import ToolOperationRegistry
from mship.util.shell import ShellRunner


def test_generic_capture_uses_live_parent_context_and_rejects_caller_retarget(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    script = repo / "capture.py"
    script.write_text(
        "import json,os,pathlib\n"
        "context=json.loads(pathlib.Path(os.environ['MSHIP_TARGET_CONTEXT_FILE']).read_text())\n"
        "out=pathlib.Path(os.environ['MSHIP_CAPTURE_DIR'])\n"
        "(out/'layout.json').write_text(json.dumps({'target':context['private_binding']['target'],"
        "'greeting':context['options']['custom_greeting']['word']}))\n"
    )
    (repo / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  capture:\n    cmds:\n"
        f"      - '{sys.executable} capture.py'\n"
    )
    config = WorkspaceConfig(
        workspace="capture",
        run_hosts=["web"],
        repos={
            "app": RepoConfig(
                path=repo,
                type="service",
                tasks={"capture": "capture", "run": "run", "targets": "targets"},
                run_profiles={
                    "browser": {
                        "backend": "custom",
                        "hosts": {"roles": ["web"]},
                        "options": {"custom_greeting": {"word": "hello"}},
                    }
                },
                run_backends={
                    "custom": {
                        "discover_task": "targets",
                        "operations": {"run": "run", "capture": "capture"},
                    }
                },
            )
        },
    )
    source = "a" * 40
    configured = config.repos["app"]
    request = DiscoveryRequest(
        protocol_version=1,
        task="demo",
        repo="app",
        backend="custom",
        backend_revision=source,
        profile="browser",
        profile_revision=profile_revision(
            configured.run_profiles["browser"],
            configured.run_backends["custom"],
            prepared_source_revision=source,
        ),
        operation="capture",
        options={"custom_greeting": {"word": "hello"}},
        target_alias=None,
    )
    context = {
        **request.model_dump(mode="json"),
        "operation": "run",
        "run_id": "one",
        "session_owner": None,
        "capabilities": ["run", "capture"],
        "private_binding": {"platform": "browser", "target": "original"},
    }
    registry = ToolOperationRegistry(tmp_path)
    parent = registry.run(
        ToolRequest(
            task="demo",
            repo="app",
            argv=(sys.executable, "-c", "import time; time.sleep(60)"),
            source_revision=source,
            input_files={
                "MSHIP_TARGET_CONTEXT_FILE": json.dumps(context),
                "MSHIP_TARGET_BINDINGS_FILE": json.dumps({"paths": {}, "aliases": {}}),
            },
        ),
        ToolContext("demo", "app", repo, source),
    )
    owner = next(parent).result
    assert owner is not None
    operation = ToolRequest(
        task="demo",
        repo="app",
        argv=(),
        task_key="capture",
        preparation="observe",
        source_revision=source,
        owner_ref=owner.owner_ref,
        generation=owner.generation,
        input_files={"MSHIP_TARGET_REQUEST_FILE": request.model_dump_json()},
    )
    deps = RemoteExecDeps(
        config=config, shell=ShellRunner(), workspace_root=tmp_path, operations=registry
    )
    try:
        output = b"".join(
            run_observe_capture_stream(
                operation,
                deps=deps,
                kinds=["layout"],
                platform="browser",
                nonce="proof",
            )
        )
        assert output.endswith(b"__MSHIP_EXIT__:proof 0\n"), output
        header, payload = output.split(b"\n", 1)
        size = int(header.split()[-1])
        with tarfile.open(fileobj=io.BytesIO(payload[:size])) as archive:
            extracted = archive.extractfile("layout.json")
            assert extracted is not None
            assert json.load(extracted) == {"target": "original", "greeting": "hello"}
        forged = replace(
            operation,
            input_files={
                **operation.input_files,
                "MSHIP_TARGET_CONTEXT_FILE": json.dumps(
                    {**context, "private_binding": {"target": "replacement"}}
                ),
            },
        )
        rejected = b"".join(
            run_observe_capture_stream(
                forged, deps=deps, kinds=["layout"], platform="browser", nonce="proof"
            )
        )
        assert rejected.endswith(b"__MSHIP_EXIT__:proof 1\n")
        assert b"__MSHIP_ARTIFACTS__" not in rejected
        assert (
            registry.status(
                task="demo",
                repo="app",
                owner_ref=owner.owner_ref,
                generation=owner.generation,
            ).status
            == "running"
        )
    finally:
        registry.stop_owner(
            task="demo",
            repo="app",
            owner_ref=owner.owner_ref,
            generation=owner.generation,
            source_revision=source,
        )
        list(parent)


def test_example_private_bindings_reject_duplicate_fields_and_oversized_objects(
    tmp_path, monkeypatch
):
    path = tmp_path / "bindings.json"
    path.write_text('{"paths":{},"aliases":{},"aliases":{}}')
    path.chmod(0o600)
    monkeypatch.setenv("MSHIP_TARGET_BINDINGS_FILE", str(path))
    with pytest.raises(common.ExampleError):
        common.load_bindings()

    path.write_text(
        json.dumps({"paths": {"custom": "x" * (1024 * 1024)}, "aliases": {}})
    )
    with pytest.raises(common.ExampleError):
        common.load_bindings()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX private inputs")
def test_example_private_bindings_reject_fifo_without_waiting_for_a_writer(tmp_path):
    fifo = tmp_path / "bindings.fifo"
    os.mkfifo(fifo, 0o600)
    script = (
        "from mship.backends.common import ExampleError, load_bindings\n"
        "try:\n"
        "    load_bindings()\n"
        "except ExampleError:\n"
        "    raise SystemExit(23)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env={**os.environ, "MSHIP_TARGET_BINDINGS_FILE": str(fifo)},
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 23, result.stderr
