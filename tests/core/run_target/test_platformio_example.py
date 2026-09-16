"""Behavioral contracts for the opt-in PlatformIO run-target example."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mship.backends.platformio import backend as platformio_backend

_FIXTURES = Path(__file__).with_name("fixtures") / "platformio"


def _backend():
    return platformio_backend


def _request(
    operation: str = "run", *, environment: str | None = "lab"
) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "backend": "platformio-example",
        "backend_revision": "a" * 40,
        "profile": "platformio-lab",
        "profile_revision": "b" * 64,
        "task": "task-1",
        "repo": "firmware",
        "operation": operation,
        "options": {} if environment is None else {"environment": environment},
        "target_alias": None,
    }


def _devices() -> list[dict[str, object]]:
    return json.loads((_FIXTURES / "device-list-redacted.json").read_text())


def _environments() -> dict[str, dict[str, str]]:
    raw = json.loads((_FIXTURES / "project-config-redacted.json").read_text())
    return {
        key.removeprefix("env:"): {
            "name": key.removeprefix("env:"),
            "platform": value["platform"],
            "board": value["board"],
        }
        for key, value in raw.items()
    }


def _bindings() -> dict[str, object]:
    return {
        "paths": {
            "platformio": {
                "executable": "/opt/platformio/pio",
                "project_dir": "/tmp/firmware",
                "monitor_state_dir": "/tmp/platformio-state",
            }
        },
        "aliases": {"platformio": {"lab-board": "10C4:EA60:REDACTED-BOARD-01"}},
    }


def test_discovery_uses_only_read_only_inventory_and_never_provisions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    backend, calls, emitted = _backend(), [], {}
    request = _request(environment=None)
    monkeypatch.setattr(backend, "load_request", lambda: request)
    monkeypatch.setattr(backend, "load_bindings", _bindings)
    monkeypatch.setattr(
        backend,
        "_configured",
        lambda _bindings: ("/opt/platformio/pio", tmp_path, tmp_path),
    )
    project = json.loads(
        (_FIXTURES / "project-config-redacted.json").read_text()
    ).copy()
    devices = _devices()

    def readonly(argv, *, cwd):
        calls.append(tuple(argv))
        assert cwd == tmp_path
        if argv[1:3] == ("project", "config"):
            return json.dumps(project).encode()
        if argv[1:3] == ("device", "list"):
            return json.dumps(devices).encode()
        raise AssertionError(f"unexpected PlatformIO operation: {argv}")

    monkeypatch.setattr(backend, "_run", readonly)
    monkeypatch.setattr(
        backend,
        "emit_inventory",
        lambda _request, candidates, rank_schema=(), errors=(): emitted.update(
            candidates=list(candidates), errors=list(errors)
        ),
    )

    backend.discover()

    assert calls == [
        ("/opt/platformio/pio", "project", "config", "--json-output"),
        ("/opt/platformio/pio", "device", "list", "--json-output"),
    ]
    assert {
        candidate["binding"]["environment"]["name"]
        for candidate in emitted["candidates"]
        if candidate["ready"]
    } == {"lab", "release"}
    assert not emitted["errors"]


def test_discovery_never_selects_a_port_without_vid_pid_and_serial(
    monkeypatch: pytest.MonkeyPatch,
):
    backend, emitted = _backend(), {}
    monkeypatch.setattr(backend, "load_request", lambda: _request(environment="lab"))
    monkeypatch.setattr(backend, "load_bindings", _bindings)
    monkeypatch.setattr(
        backend,
        "_configured",
        lambda _bindings: (
            "/opt/platformio/pio",
            Path("/tmp/firmware"),
            Path("/tmp/platformio-state"),
        ),
    )
    monkeypatch.setattr(
        backend,
        "_inventory",
        lambda _request, _pio, _project: ((_environments()["lab"],), tuple(_devices())),
    )
    monkeypatch.setattr(
        backend,
        "emit_inventory",
        lambda _request, candidates, rank_schema=(), errors=(): emitted.update(
            candidates=list(candidates)
        ),
    )

    backend.discover()

    unready = [
        candidate for candidate in emitted["candidates"] if not candidate["ready"]
    ]
    assert len(unready) == 1
    assert unready[0]["reason"] == "identity-unknown"
    assert unready[0]["capabilities"] == []
    assert "port" not in unready[0]["binding"]


def test_revalidation_accepts_a_changed_port_only_for_the_same_physical_board(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    backend = _backend()
    request = _request()
    physical = {"vid": "10C4", "pid": "EA60", "serial": "REDACTED-BOARD-01"}
    environment = _environments()["lab"]
    sealed = {
        "target_key": backend._target_key(physical, environment),
        "platform": "platformio",
        "physical": physical,
        "port": "/dev/ttyACM0",
        "environment": environment,
    }
    monkeypatch.setattr(backend, "load_context", lambda: {"private_binding": sealed})
    monkeypatch.setattr(
        backend,
        "_configured",
        lambda _bindings: ("/opt/platformio/pio", tmp_path, tmp_path),
    )
    config = json.loads((_FIXTURES / "project-config-redacted.json").read_text())
    changed_port = _devices()
    changed_port[0]["port"] = "/dev/ttyUSB42"

    def inventory(argv, *, cwd):
        if argv[1:3] == ("project", "config"):
            return json.dumps(config).encode()
        return json.dumps(changed_port).encode()

    monkeypatch.setattr(backend, "_run", inventory)
    _executable, _project, _state_directory, selected = backend._selected(
        request, _bindings()
    )
    assert selected["port"] == "/dev/ttyUSB42"

    changed_port[0]["hwid"] = (
        "USB VID:PID=10C4:EA60 SER=REDACTED-BOARD-OTHER LOCATION=1-2"
    )
    with pytest.raises(backend.BackendError, match="identity"):
        backend._selected(request, _bindings())


def test_monitor_owner_log_reuses_same_physical_board_after_port_change(tmp_path: Path):
    backend = _backend()
    physical = {"vid": "10C4", "pid": "EA60", "serial": "REDACTED-BOARD-01"}
    environment = _environments()["lab"]
    target_key = backend._target_key(physical, environment)
    previous = {
        "target_key": target_key,
        "physical": physical,
        "environment": environment,
        "port": "/dev/ttyACM0",
    }
    current = {**previous, "port": "/dev/ttyUSB42"}
    owner = tmp_path / "owner.json"
    owner.write_text(json.dumps({"run_id": "run-1", "binding": previous}))

    backend._owner(owner, current, "run-1")


def test_board_lock_is_shared_across_platformio_environments(tmp_path: Path):
    backend = _backend()
    physical = {"vid": "10C4", "pid": "EA60", "serial": "REDACTED-BOARD-01"}
    environments = _environments()
    lab_key = backend._target_key(physical, environments["lab"])
    release_key = backend._target_key(physical, environments["release"])

    lab_lock, _, _ = backend._monitor_paths(
        tmp_path,
        "run-lab",
        physical,
        lab_key,
        create=True,
    )
    release_lock, _, _ = backend._monitor_paths(
        tmp_path,
        "run-release",
        physical,
        release_key,
        create=True,
    )

    assert lab_lock == release_lock


def test_monitor_operation_never_routes_to_upload(monkeypatch: pytest.MonkeyPatch):
    backend, calls = _backend(), []
    request = _request("run")
    binding = {
        "target_key": "platformio-target",
        "environment": _environments()["lab"],
        "port": "/dev/ttyACM0",
    }
    state_directory = Path("/tmp/platformio-state")
    monkeypatch.setattr(
        backend,
        "_selected",
        lambda _request, _bindings: (
            "/opt/platformio/pio",
            Path("/tmp/firmware"),
            state_directory,
            binding,
        ),
    )
    monkeypatch.setattr(backend, "load_context", lambda: {"run_id": "run-1"})
    monkeypatch.setattr(backend, "_monitor", lambda *args: calls.append(args))
    monkeypatch.setattr(
        backend,
        "_upload_argv",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("upload inferred from monitor")
        ),
    )

    backend.run(request, _bindings())

    assert calls == [
        (
            "/opt/platformio/pio",
            Path("/tmp/firmware"),
            state_directory,
            binding,
            {"run_id": "run-1"},
        )
    ]
    assert backend._monitor_argv("/opt/platformio/pio", binding)[-1] == "--no-reconnect"


def test_monitor_readiness_requires_platformio_admission_for_the_selected_port():
    backend = _backend()

    assert not backend._monitor_admitted(
        b"--- Terminal on /dev/ttyUSB1 | 115200 8-N-1", "/dev/ttyACM0"
    )
    assert backend._monitor_admitted(
        b"--- Terminal on /dev/ttyACM0 | 115200 8-N-1", "/dev/ttyACM0"
    )


def test_upload_is_routed_only_by_the_typed_upload_operation(
    monkeypatch: pytest.MonkeyPatch,
):
    backend, calls = _backend(), []
    request = _request("upload")
    monkeypatch.setattr(backend, "load_request", lambda: request)
    monkeypatch.setattr(backend, "load_bindings", _bindings)
    monkeypatch.setattr(
        backend,
        "upload",
        lambda received, bindings: calls.append((received["operation"], bindings)),
    )
    monkeypatch.setattr(
        backend,
        "run",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("monitor selected for upload")
        ),
    )

    assert backend.main(["backend.py"]) == 0
    assert calls == [("upload", _bindings())]
