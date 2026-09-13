import json
import os
import selectors
import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest

from mship.core.run_host.config import HostRegistration, RunHostConnection
from mship.core.run_target.backend import (
    DISCOVERY_STDOUT_LIMIT,
    discover_on_host,
    host_bindings_path,
    load_host_bindings,
    parse_discovery_result,
)
from mship.core.run_target.models import (
    BackendConfig,
    BackendExecution,
    BackendResult,
    DiscoveryRequest,
    TargetSelectionError,
)
from mship.core.run_target.selection import rank_targets


def _execute_child(
    script: Path, request_path: Path, execution: BackendExecution
) -> BackendResult:
    """Bounded collection for these isolated, single-child protocol fixtures."""
    stdout_limit = execution.max_stdout_bytes
    stderr_limit = execution.max_stderr_bytes
    timeout = execution.timeout_seconds
    assert stdout_limit is not None and stderr_limit is not None and timeout is not None
    deadline = monotonic() + timeout
    request_path.write_text(json.dumps(execution.request))
    request_path.chmod(0o600)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": stdout_limit, "stderr": stderr_limit}
    error = None
    with subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env={
            "MSHIP_TARGET_REQUEST_FILE": str(request_path),
            "PATH": os.environ["PATH"],
        },
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(process.stdout, selectors.EVENT_READ, "stdout")
                ready.register(process.stderr, selectors.EVENT_READ, "stderr")
                while ready.get_map() and error is None:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        error = "timeout"
                        break
                    for key, _ in ready.select(remaining):
                        channel = key.data
                        capacity = limits[channel] - len(buffers[channel])
                        chunk = os.read(key.fd, min(65536, capacity + 1))
                        if not chunk:
                            ready.unregister(key.fileobj)
                        elif len(chunk) > capacity:
                            error = f"{channel}_limit"
                            break
                        else:
                            buffers[channel].extend(chunk)
            if error is None:
                try:
                    process.wait(timeout=max(0, deadline - monotonic()))
                except subprocess.TimeoutExpired:
                    error = "timeout"
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    return BackendResult(
        exit_code=process.returncode,
        stdout=bytes(buffers["stdout"]),
        stderr=bytes(buffers["stderr"]),
        error_code=error,
        owner_ref=None,
        owner_generation=None,
        artifacts=(),
    )


def _host(name: str = "studio") -> HostRegistration:
    return HostRegistration(
        name=name,
        roles=("mobile",),
        tags=(),
        preference=0,
        connection=RunHostConnection(f"https://{name}.invalid", "secret"),
        scope="project",
    )


def _request(**changes: object) -> DiscoveryRequest:
    values = dict(
        protocol_version=1,
        backend="example",
        backend_revision="adapter-a",
        profile="ios",
        profile_revision="profile-a",
        task="work",
        repo="app",
        operation="run",
        options={"platform": "ios"},
        target_alias="phone",
    )
    values.update(changes)
    return DiscoveryRequest(**values)


def _payload(**changes: object) -> bytes:
    values = dict(
        protocol_version=1,
        backend="example",
        backend_revision="adapter-a",
        rank_schema=["major"],
        candidates=[
            {
                "target_key": "private-device-id",
                "label": "iPhone",
                "tags": ["ios"],
                "roles": ["mobile"],
                "aliases": ["phone"],
                "capabilities": ["run"],
                "ready": True,
                "reason": None,
                "remediation": None,
                "preparation": [],
                "rank": [18],
                "binding": {"device": "private-device-id"},
            }
        ],
        errors=[],
    )
    values.update(changes)
    return json.dumps(values).encode()


def test_discovery_cannot_claim_another_host():
    payload = {
        "protocol_version": 1,
        "backend": "example",
        "backend_revision": "adapter-a",
        "rank_schema": [],
        "candidates": [],
        "errors": [],
        "host": "privileged-host",
    }
    with pytest.raises(TargetSelectionError) as error:
        parse_discovery_result(json.dumps(payload).encode(), max_bytes=1024)
    assert error.value.code == "backend_protocol"


@pytest.mark.parametrize(
    "payload",
    [
        _payload(
            candidates=[
                json.loads(_payload())["candidates"][0],
                json.loads(_payload())["candidates"][0],
            ]
        ),
        _payload(
            candidates=[{**json.loads(_payload())["candidates"][0], "rank": [1, 2]}]
        ),
        _payload(
            candidates=[
                {**json.loads(_payload())["candidates"][0], "rank": [float("nan")]}
            ]
        ),
    ],
)
def test_parser_rejects_duplicate_or_invalid_rank_protocol(payload: bytes):
    with pytest.raises(TargetSelectionError) as error:
        parse_discovery_result(payload, max_bytes=DISCOVERY_STDOUT_LIMIT)
    assert error.value.code == "backend_protocol"


def test_parser_rejects_oversized_output_without_returning_private_data():
    with pytest.raises(TargetSelectionError) as error:
        parse_discovery_result(b"x" * 33, max_bytes=32)
    assert error.value.code == "backend_protocol"
    assert "x" * 33 not in str(error.value)


@pytest.mark.parametrize(
    "changes", [{"backend": "other"}, {"backend_revision": "other"}]
)
def test_discover_rejects_exact_backend_identity_mismatch(changes: dict[str, str]):
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        return BackendResult(
            exit_code=0,
            stdout=_payload(**changes),
            stderr=b"",
            error_code=None,
            owner_ref=None,
            owner_generation=None,
            artifacts=(),
        )

    assert (
        discover_on_host(_host(), _request(), config, execute=execute).error
        == "backend_protocol"
    )


def test_discover_explicit_alias_returns_a_complete_empty_inventory_when_host_has_no_match():
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        return BackendResult(
            exit_code=0,
            stdout=_payload(),
            stderr=b"",
            error_code=None,
            owner_ref=None,
            owner_generation=None,
            artifacts=(),
        )

    inventory = discover_on_host(
        _host(), _request(target_alias="missing"), config, execute=execute
    )
    assert inventory.error is None
    assert inventory.candidates == ()


def test_explicit_alias_filters_before_global_ranking_across_hosts():
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})
    base = json.loads(_payload())["candidates"][0]
    high_nonmatching = {
        **base,
        "target_key": "private-high",
        "aliases": ["other"],
        "rank": [20],
    }
    matching = {
        **base,
        "target_key": "private-phone",
        "aliases": ["phone"],
        "rank": [19],
    }
    other_host = {
        **base,
        "target_key": "private-other-host",
        "aliases": ["other"],
        "rank": [21],
    }

    def execute(host, execution):
        candidates = (
            [high_nonmatching, matching] if host.name == "studio" else [other_host]
        )
        return BackendResult(
            exit_code=0,
            stdout=_payload(candidates=candidates),
            stderr=b"",
            error_code=None,
            owner_ref=None,
            owner_generation=None,
            artifacts=(),
        )

    first = discover_on_host(_host("studio"), _request(), config, execute=execute)
    second = discover_on_host(_host("air"), _request(), config, execute=execute)
    winners = rank_targets(
        (first, second), profile_revision="profile-a", operation="run"
    )
    assert [(winner.host.name, winner.candidate.aliases) for winner in winners] == [
        ("studio", ("phone",))
    ]


def test_backend_reported_error_marks_inventory_incomplete_even_with_candidates():
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        return BackendResult(
            exit_code=0,
            stdout=_payload(
                errors=[
                    {
                        "code": "busy",
                        "message": "target is busy",
                        "remediation": "retry later",
                    }
                ]
            ),
            stderr=b"private details",
            error_code=None,
            owner_ref=None,
            owner_generation=None,
            artifacts=(),
        )

    inventory = discover_on_host(_host(), _request(), config, execute=execute)
    assert inventory.error == "backend_reported_error"
    assert len(inventory.candidates) == 1
    assert inventory.operation == "run"
    assert inventory.profile_revision == "profile-a"


def test_discover_requires_exact_backend_revision_and_alias():
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        return BackendResult(
            exit_code=0,
            stdout=_payload(backend_revision="other"),
            stderr=b"",
            error_code=None,
            owner_ref=None,
            owner_generation=None,
            artifacts=(),
        )

    inventory = discover_on_host(_host(), _request(), config, execute=execute)
    assert inventory.error == "backend_protocol"
    assert inventory.candidates == ()


def test_discover_normalizes_transport_error_without_exposing_stderr():
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        return BackendResult(
            exit_code=None,
            stdout=b"private stdout",
            stderr=b"token=private",
            error_code="transport",
            owner_ref=None,
            owner_generation=None,
            artifacts=(),
        )

    inventory = discover_on_host(_host(), _request(), config, execute=execute)
    assert inventory.error == "backend_transport"
    assert inventory.candidates == ()
    assert inventory.operation == "run"
    assert inventory.profile_revision == "profile-a"


def test_discovery_script_consumes_private_request_and_emits_protocol(tmp_path: Path):
    script = tmp_path / "discover.py"
    script.write_text(
        "import json, os, sys\n"
        "request = json.load(open(os.environ['MSHIP_TARGET_REQUEST_FILE']))\n"
        "assert request['backend'] == 'example'\n"
        "print(json.dumps({'protocol_version': 1, 'backend': request['backend'], "
        "'backend_revision': request['backend_revision'], 'rank_schema': [], 'candidates': [{"
        "'target_key': 'device-private', 'label': 'Phone', 'tags': [], 'roles': [], 'aliases': ['phone'], "
        "'capabilities': ['run'], 'ready': True, 'reason': None, 'remediation': None, 'preparation': [], 'rank': [], 'binding': {}}], 'errors': []}))\n"
    )
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        return _execute_child(script, tmp_path / "request.json", execution)

    inventory = discover_on_host(_host(), _request(), config, execute=execute)
    assert inventory.error is None
    assert inventory.host.name == "studio"
    assert inventory.candidates[0].aliases == ("phone",)
    assert inventory.operation == "run"
    assert inventory.profile_revision == "profile-a"


@pytest.mark.parametrize(
    ("channel", "expected"),
    [
        ("stdout", "backend_protocol"),
        ("stderr", "backend_protocol"),
        ("quiet", "backend_timeout"),
    ],
)
def test_discovery_executor_enforces_limits_before_child_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str, expected: str
):
    monkeypatch.setattr("mship.core.run_target.backend.DISCOVERY_STDOUT_LIMIT", 512)
    monkeypatch.setattr("mship.core.run_target.backend.DISCOVERY_STDERR_LIMIT", 64)
    monkeypatch.setattr(
        "mship.core.run_target.backend.DISCOVERY_TIMEOUT_SECONDS",
        0.2 if channel == "quiet" else 2,
    )
    script = tmp_path / "unbounded.py"
    output = ""
    if channel != "quiet":
        size = 513 if channel == "stdout" else 65
        descriptor = 1 if channel == "stdout" else 2
        output = f"os.write({descriptor}, (b'private-target' * {size})[:{size}])\n"
    script.write_text("import os, time\n" + output + "time.sleep(60)\n")
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})
    results = []

    def execute(host, execution):
        result = _execute_child(script, tmp_path / "request.json", execution)
        results.append(result)
        return result

    inventory = discover_on_host(_host(), _request(), config, execute=execute)
    assert inventory.error == expected
    assert inventory.candidates == ()
    assert "private-target" not in repr(inventory)
    assert len(results[0].stdout) <= 512
    assert len(results[0].stderr) <= 64


def test_bindings_require_owner_private_regular_file_and_stay_bounded(tmp_path: Path):
    bindings = tmp_path / "run-target-bindings.yaml"
    bindings.write_text(
        "version: 1\nbackends:\n  flutter:\n    paths: {}\n    aliases: {}\n"
    )
    bindings.chmod(0o644)
    with pytest.raises(TargetSelectionError) as error:
        load_host_bindings(bindings, "flutter")
    assert error.value.code == "backend_protocol"

    bindings.chmod(0o600)
    link = tmp_path / "bindings-link.yaml"
    link.symlink_to(bindings)
    with pytest.raises(TargetSelectionError) as error:
        load_host_bindings(link, "flutter")
    assert error.value.code == "backend_protocol"

    fifo = tmp_path / "bindings.fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(TargetSelectionError) as error:
        load_host_bindings(fifo, "flutter")
    assert error.value.code == "backend_protocol"

    bindings.write_bytes(b"x" * (1024 * 1024 + 1))
    bindings.chmod(0o600)
    with pytest.raises(TargetSelectionError) as error:
        load_host_bindings(bindings, "flutter")
    assert error.value.code == "backend_protocol"


def test_bindings_only_return_requested_backend_and_reject_non_json(tmp_path: Path):
    bindings = tmp_path / "run-target-bindings.yaml"
    bindings.write_text(
        "version: 1\nbackends:\n  flutter:\n    paths: {sdk: /private/flutter}\n    aliases: {phone: device-private}\n  browser:\n    paths: {binary: /private/browser}\n    aliases: {}\n"
    )
    bindings.chmod(0o600)
    assert load_host_bindings(bindings, "flutter") == {
        "paths": {"sdk": "/private/flutter"},
        "aliases": {"phone": "device-private"},
    }
    bindings.write_text(
        "version: 1\nbackends:\n  flutter:\n    paths: {bad: !!set {x: null}}\n    aliases: {}\n"
    )
    bindings.chmod(0o600)
    with pytest.raises(TargetSelectionError) as error:
        load_host_bindings(bindings, "flutter")
    assert error.value.code == "backend_protocol"


def test_bindings_path_ignores_relative_or_empty_xdg_values(tmp_path: Path):
    home = tmp_path / "home"
    assert (
        host_bindings_path(home, {"XDG_CONFIG_HOME": ""})
        == home / ".config" / "mothership" / "run-target-bindings.yaml"
    )
    assert (
        host_bindings_path(home, {"XDG_CONFIG_HOME": "relative"})
        == home / ".config" / "mothership" / "run-target-bindings.yaml"
    )
    assert host_bindings_path(home, {"XDG_CONFIG_HOME": "/private/config"}) == Path(
        "/private/config/mothership/run-target-bindings.yaml"
    )
