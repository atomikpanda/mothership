import json
import os
import subprocess
import sys
from pathlib import Path

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
    BackendResult,
    DiscoveryRequest,
    TargetSelectionError,
)


def _host() -> HostRegistration:
    return HostRegistration(
        name="studio", roles=("mobile",), tags=(), preference=0,
        connection=RunHostConnection("https://studio.invalid", "secret"), scope="project",
    )


def _request(**changes: object) -> DiscoveryRequest:
    values = dict(
        protocol_version=1, backend="example", backend_revision="adapter-a",
        profile="ios", profile_revision="profile-a", task="work", repo="app",
        operation="run", options={"platform": "ios"}, target_alias="phone",
    )
    values.update(changes)
    return DiscoveryRequest(**values)


def _payload(**changes: object) -> bytes:
    values = dict(
        protocol_version=1, backend="example", backend_revision="adapter-a", rank_schema=["major"],
        candidates=[{
            "target_key": "private-device-id", "label": "iPhone", "tags": ["ios"],
            "roles": ["mobile"], "aliases": ["phone"], "capabilities": ["run"],
            "ready": True, "reason": None, "remediation": None, "preparation": [],
            "rank": [18], "binding": {"device": "private-device-id"},
        }],
        errors=[],
    )
    values.update(changes)
    return json.dumps(values).encode()


def test_discovery_cannot_claim_another_host():
    payload = {
        "protocol_version": 1, "backend": "example", "backend_revision": "adapter-a",
        "rank_schema": [], "candidates": [], "errors": [], "host": "privileged-host",
    }
    with pytest.raises(TargetSelectionError) as error:
        parse_discovery_result(json.dumps(payload).encode(), max_bytes=1024)
    assert error.value.code == "backend_protocol"


@pytest.mark.parametrize(
    "payload",
    [
        _payload(candidates=[json.loads(_payload())["candidates"][0], json.loads(_payload())["candidates"][0]]),
        _payload(candidates=[{**json.loads(_payload())["candidates"][0], "rank": [1, 2]}]),
        _payload(candidates=[{**json.loads(_payload())["candidates"][0], "rank": [float("nan")]}]),
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


@pytest.mark.parametrize("changes", [{"backend": "other"}, {"backend_revision": "other"}])
def test_discover_rejects_exact_backend_identity_mismatch(changes: dict[str, str]):
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        return BackendResult(exit_code=0, stdout=_payload(**changes), stderr=b"", error_code=None, owner_ref=None, owner_generation=None, artifacts=())

    assert discover_on_host(_host(), _request(), config, execute=execute).error == "backend_protocol"


def test_discover_rejects_explicit_alias_not_advertised_by_backend():
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        return BackendResult(exit_code=0, stdout=_payload(), stderr=b"", error_code=None, owner_ref=None, owner_generation=None, artifacts=())

    assert discover_on_host(_host(), _request(target_alias="missing"), config, execute=execute).error == "target_unavailable"


def test_backend_reported_error_marks_inventory_incomplete_even_with_candidates():
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        return BackendResult(
            exit_code=0,
            stdout=_payload(errors=[{"code": "busy", "message": "target is busy", "remediation": "retry later"}]),
            stderr=b"private details",
            error_code=None,
            owner_ref=None,
            owner_generation=None,
            artifacts=(),
        )

    inventory = discover_on_host(_host(), _request(), config, execute=execute)
    assert inventory.error == "backend_reported_error"
    assert len(inventory.candidates) == 1

def test_discover_requires_exact_backend_revision_and_alias():
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        return BackendResult(exit_code=0, stdout=_payload(backend_revision="other"), stderr=b"", error_code=None, owner_ref=None, owner_generation=None, artifacts=())

    inventory = discover_on_host(_host(), _request(), config, execute=execute)
    assert inventory.error == "backend_protocol"
    assert inventory.candidates == ()


def test_discover_normalizes_transport_error_without_exposing_stderr():
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        return BackendResult(exit_code=None, stdout=b"private stdout", stderr=b"token=private", error_code="transport", owner_ref=None, owner_generation=None, artifacts=())

    inventory = discover_on_host(_host(), _request(), config, execute=execute)
    assert inventory.error == "backend_transport"
    assert inventory.candidates == ()


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
        "print('read-only inventory', file=sys.stderr)\n"
    )
    config = BackendConfig(discover_task="discover", operations={"run": "launch"})

    def execute(host, execution):
        request_path = tmp_path / "request.json"
        request_path.write_text(json.dumps(execution.request))
        result = subprocess.run(
            [sys.executable, str(script)], capture_output=True, check=False,
            env={"MSHIP_TARGET_REQUEST_FILE": str(request_path), "PATH": os.environ["PATH"]},
        )
        return BackendResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr, error_code=None, owner_ref=None, owner_generation=None, artifacts=())

    inventory = discover_on_host(_host(), _request(), config, execute=execute)
    assert inventory.error is None
    assert inventory.host.name == "studio"
    assert inventory.candidates[0].aliases == ("phone",)


def test_bindings_only_return_requested_backend_and_reject_non_json(tmp_path: Path):
    bindings = tmp_path / "run-target-bindings.yaml"
    bindings.write_text(
        "version: 1\nbackends:\n  flutter:\n    paths: {sdk: /private/flutter}\n    aliases: {phone: device-private}\n  browser:\n    paths: {binary: /private/browser}\n    aliases: {}\n"
    )
    assert load_host_bindings(bindings, "flutter") == {
        "paths": {"sdk": "/private/flutter"}, "aliases": {"phone": "device-private"}
    }
    bindings.write_text("version: 1\nbackends:\n  flutter:\n    paths: {bad: !!set {x: null}}\n    aliases: {}\n")
    with pytest.raises(TargetSelectionError) as error:
        load_host_bindings(bindings, "flutter")
    assert error.value.code == "backend_protocol"


def test_bindings_path_ignores_relative_or_empty_xdg_values(tmp_path: Path):
    home = tmp_path / "home"
    assert host_bindings_path(home, {"XDG_CONFIG_HOME": ""}) == home / ".config" / "mothership" / "run-target-bindings.yaml"
    assert host_bindings_path(home, {"XDG_CONFIG_HOME": "relative"}) == home / ".config" / "mothership" / "run-target-bindings.yaml"
    assert host_bindings_path(home, {"XDG_CONFIG_HOME": "/private/config"}) == Path("/private/config/mothership/run-target-bindings.yaml")
