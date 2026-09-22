from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import pytest

from mship.core.flutter_adapter import (
    FlutterLaunchConfig,
    FlutterOptions,
    FlutterTargetBinding,
    attest_ios_foreground,
    redact_log,
)
from mship.core.flutter_session import FlutterSessionOwner
from mship.core.session_inputs import OwnerRequest, SessionError


_SOURCE = "a" * 40
_NEXT_SOURCE = "b" * 40
_MACHINE_APP_ID = "machine-owned-app-42"
_PLATFORM_APP_ID = "com.example.platformapp"
_DEVICE_ID = "ios-device-0001"


@pytest.fixture
def flutter_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Bounded Flutter-machine and platform receipt executables for owner lifecycle tests."""
    machine_state = tmp_path / "machine-state.jsonl"
    log_signal = tmp_path / "emit-log"
    receipt_device = tmp_path / "receipt-device"
    receipt_device.write_text(_DEVICE_ID)
    machine = tmp_path / "fake-flutter"
    machine.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import select
import sys

state = Path(os.environ["FAKE_FLUTTER_STATE"])
log_signal = Path(os.environ["FAKE_FLUTTER_LOG_SIGNAL"])
machine_app_id = "machine-owned-app-42"

def record(kind, **values):
    with state.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": kind, **values}, separators=(",", ":")) + "\\n")
        stream.flush()

def emit(records):
    sys.stdout.write(json.dumps(records, separators=(",", ":")) + "\\n")
    sys.stdout.flush()

if sys.argv[1:] == ["devices", "--machine"]:
    print(json.dumps([{"id": "ios-device-0001", "targetPlatform": "ios",
                       "capabilities": {"hotReload": True, "hotRestart": True}}]))
    raise SystemExit(0)

record("started", machine_app_id=machine_app_id)
emit([
    {"event": "app.start", "params": {"appId": machine_app_id, "directory": os.getcwd(),
                                        "deviceId": "ios-device-0001", "launchMode": "run",
                                        "mode": "debug"}},
    {"event": "app.started", "params": {"appId": machine_app_id}},
])
logs_remaining = 10
while True:
    if log_signal.exists() and logs_remaining:
        logs_remaining -= 1
        emit([{"event": "app.log", "params": {"appId": machine_app_id, "log": "ready"}}])
    readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.02)
    if not readable:
        continue
    line = sys.stdin.buffer.readline()
    if not line:
        raise SystemExit(0)
    for command in json.loads(line):
        method = command.get("method")
        if method == "app.restart":
            params = command["params"]
            record("reload-attempt", machine_app_id=params.get("appId"),
                   full_restart=params.get("fullRestart"))
            acknowledgement = os.environ.get("FAKE_FLUTTER_ACK", "valid")
            if acknowledgement == "valid":
                record("reload-applied", machine_app_id=params.get("appId"))
                emit([{"id": command["id"], "result": {"code": 0, "message": "reloaded"}}])
            elif acknowledgement == "malformed":
                emit([{"id": command["id"], "result": {"code": "0", "message": "reloaded"}}])
        elif method == "app.stop":
            emit([
                {"id": command["id"], "result": True},
                {"event": "app.stop", "params": {"appId": machine_app_id}},
            ])
            raise SystemExit(0)
"""
    )
    receipt = tmp_path / "fake-ios-receipt"
    receipt.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

device_id = Path(os.environ["FAKE_RECEIPT_DEVICE"]).read_text(encoding="utf-8").strip()
receipt = {"version": 1, "platform": "ios", "transport": "usb",
           "target_fingerprint": "ios-fingerprint", "device_id": device_id}
if sys.argv[1:] == ["foreground"]:
    receipt["app_identity"] = "com.example.platformapp"
print(json.dumps(receipt, separators=(",", ":")))
"""
    )
    for executable in (machine, receipt):
        executable.chmod(0o700)
    monkeypatch.setenv("FAKE_FLUTTER_STATE", str(machine_state))
    monkeypatch.setenv("FAKE_FLUTTER_LOG_SIGNAL", str(log_signal))
    monkeypatch.setenv("FAKE_RECEIPT_DEVICE", str(receipt_device))
    return {
        "machine": machine,
        "state": machine_state,
        "log_signal": log_signal,
        "receipt": receipt,
        "receipt_device": receipt_device,
    }


def _ios_target(receipt: Path) -> FlutterTargetBinding:
    return FlutterTargetBinding.from_dict(
        {
            "target_key": "target-ios",
            "label": "iPhone",
            "tags": ["mobile"],
            "roles": ["device"],
            "aliases": ["phone"],
            "preparation": [],
            "rank": [1],
            "binding": {"device": "private"},
            "platform": "ios",
            "transport": "usb",
            "target_fingerprint": "ios-fingerprint",
            "device_id": _DEVICE_ID,
            "app_id": _PLATFORM_APP_ID,
            "modes": ["debug"],
            "flavors": [None],
            "capabilities": {"hot_reload": True, "hot_restart": True},
            "ios": {
                "probe_argv": [str(receipt), "probe"],
                "foreground_argv": [str(receipt), "foreground"],
                "capture_argv": [str(receipt), "capture"],
            },
        }
    )


def _owner(tmp_path: Path, flutter_machine: dict[str, Path]) -> FlutterSessionOwner:
    entrypoint = tmp_path / "lib" / "main.dart"
    entrypoint.parent.mkdir(exist_ok=True)
    entrypoint.write_text("void main() {}")
    return FlutterSessionOwner(
        FlutterLaunchConfig(
            str(flutter_machine["machine"]),
            _ios_target(flutter_machine["receipt"]),
            FlutterOptions("lib/main.dart", None, "debug"),
            {"run_id": "run-private-42"},
        ),
        worktree=tmp_path,
        source=_SOURCE,
    )


def _request(operation: str, source: str, *, suffix: str) -> OwnerRequest:
    return OwnerRequest(
        operation_ref=(suffix * 24)[:24],
        operation=operation,
        source_revision=source,
        expires_at=time.time() + 30,
    )


def _machine_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _events_of_kind(path: Path, kind: str) -> list[dict[str, object]]:
    return [event for event in _machine_events(path) if event["kind"] == kind]


def test_truncated_machine_frame_cannot_be_ignored_before_valid_readiness(
    tmp_path: Path, flutter_machine: dict[str, Path]
) -> None:
    executable = flutter_machine["machine"]
    executable.write_text(
        executable.read_text().replace(
            'record("started",', 'print("[{}", flush=True)\nrecord("started",', 1
        )
    )
    owner = _owner(tmp_path, flutter_machine)
    try:
        with pytest.raises(SessionError) as rejected:
            owner.start()
        assert rejected.value.code == "unknown"
    finally:
        owner.cleanup()


def test_ios_receipt_requires_selected_flutter_device_id(
    tmp_path: Path, flutter_machine: dict[str, Path]
) -> None:
    flutter_machine["receipt_device"].write_text("other-ios-device")
    target = _ios_target(flutter_machine["receipt"])
    owner = _owner(tmp_path, flutter_machine)

    with pytest.raises(SessionError) as foreground:
        attest_ios_foreground(target)
    with pytest.raises(SessionError) as raised:
        owner.start()

    assert foreground.value.code == raised.value.code == "unknown"
    assert _machine_events(flutter_machine["state"]) == []


@pytest.mark.parametrize("acknowledgement", ["malformed", "lost"])
def test_uncertain_machine_source_release_never_reports_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flutter_machine: dict[str, Path],
    acknowledgement: str,
) -> None:
    monkeypatch.setenv("FAKE_FLUTTER_ACK", acknowledgement)
    monkeypatch.setattr("mship.core.flutter_session._MACHINE_COMMAND_TIMEOUT", 0.15)
    owner = _owner(tmp_path, flutter_machine)
    owner.start()
    try:
        assert (
            owner.reserve_source_update("u" * 24, _NEXT_SOURCE)["stage"] == "reserved"
        )
        assert (
            owner.commit_source_update("u" * 24, _NEXT_SOURCE)["stage"]
            == "context-committed"
        )
        with pytest.raises(SessionError) as raised:
            owner.release_source_update("u" * 24, threading.Event())
        with pytest.raises(SessionError) as duplicate:
            owner.release_source_update("u" * 24, threading.Event())

        assert raised.value.code == "unknown"
        assert duplicate.value.code == "invalid"
        assert len(_events_of_kind(flutter_machine["state"], "reload-attempt")) == 1
        assert _events_of_kind(flutter_machine["state"], "reload-applied") == []
    finally:
        owner.cleanup()


def test_source_release_reloads_opaque_machine_identity_once_after_draining_observers(
    tmp_path: Path, flutter_machine: dict[str, Path]
) -> None:
    owner = _owner(tmp_path, flutter_machine)
    owner.start()
    subscriber_cancel = threading.Event()
    observed_log = threading.Event()
    subscriber_errors: list[SessionError] = []

    def observe_logs() -> None:
        try:
            owner.handle(
                _request("logs", _SOURCE, suffix="observer"),
                {},
                subscriber_cancel,
                lambda event: (
                    observed_log.set() if event["message"] == "ready" else None
                ),
            )
        except SessionError as error:
            subscriber_errors.append(error)

    subscriber = threading.Thread(target=observe_logs)
    subscriber.start()
    flutter_machine["log_signal"].touch()
    try:
        assert observed_log.wait(1)
        assert owner.reserve_source_update("u" * 24, _NEXT_SOURCE) == {
            "update_id": "u" * 24,
            "stage": "reserved",
        }
        subscriber.join(timeout=1)
        assert not subscriber.is_alive()
        assert [error.code for error in subscriber_errors] == ["cancelled"]

        with pytest.raises(SessionError) as blocked:
            owner.handle(
                _request("status", _SOURCE, suffix="blocked"),
                {},
                threading.Event(),
                lambda _event: None,
            )
        assert blocked.value.code == "busy"

        assert owner.commit_source_update("u" * 24, _NEXT_SOURCE) == {
            "update_id": "u" * 24,
            "stage": "context-committed",
        }
        assert owner.current_source_revision == _NEXT_SOURCE
        assert owner.release_source_update("u" * 24, threading.Event()) == {
            "update_id": "u" * 24,
            "stage": "reloaded",
        }
        with pytest.raises(SessionError) as duplicate:
            owner.release_source_update("u" * 24, threading.Event())
        assert duplicate.value.code == "invalid"

        reloaded = _events_of_kind(flutter_machine["state"], "reload-applied")
        assert reloaded == [
            {"kind": "reload-applied", "machine_app_id": _MACHINE_APP_ID}
        ]
        assert _MACHINE_APP_ID != _PLATFORM_APP_ID
    finally:
        subscriber_cancel.set()
        subscriber.join(timeout=1)
        owner.cleanup()


def test_flutter_options_rejects_unreviewed_mode_and_escape() -> None:
    try:
        FlutterOptions.from_dict(
            {"entrypoint": "../main.dart", "flavor": None, "mode": "debug"}
        )
    except SessionError as error:
        assert error.code == "invalid"
    else:
        raise AssertionError("escaped entrypoint was accepted")

    try:
        FlutterOptions.from_dict(
            {"entrypoint": "lib/main.dart", "flavor": None, "mode": "jit_release"}
        )
    except SessionError as error:
        assert error.code == "unavailable"
    else:
        raise AssertionError("unsupported mode was accepted")


def test_binding_requires_exact_platform_receipt_and_capabilities() -> None:
    value = {
        "target_key": "target-android",
        "label": "Android device",
        "tags": ["mobile"],
        "roles": ["device"],
        "aliases": ["phone"],
        "preparation": [],
        "rank": [1],
        "binding": {"device": "private"},
        "platform": "android",
        "transport": "usb",
        "target_fingerprint": "fingerprint",
        "device_id": "private-device",
        "app_id": "com.example.app",
        "modes": ["debug"],
        "flavors": [None],
        "capabilities": {"hot_reload": True, "hot_restart": True},
        "android": {
            "adb": "/private/adb",
            "serial": "private-device",
            "transport": "usb",
            "identity": {
                "ro_serialno": "serial",
                "build_fingerprint": "fingerprint",
                "product_device": "device",
                "api_level": 35,
                "avd_name": None,
                "usb_transport": "1-1",
            },
        },
    }
    target = FlutterTargetBinding.from_dict(value)
    assert target.supports(
        FlutterOptions.from_dict(
            {"entrypoint": "lib/main.dart", "flavor": None, "mode": "debug"}
        )
    )

    value["capabilities"] = {"hot_reload": True}
    try:
        FlutterTargetBinding.from_dict(value)
    except SessionError as error:
        assert error.code == "invalid"
    else:
        raise AssertionError("partial capability contract was accepted")


def test_machine_start_rejects_attach_or_retargeted_device(tmp_path: Path) -> None:
    entrypoint = tmp_path / "lib" / "main.dart"
    entrypoint.parent.mkdir()
    entrypoint.write_text("void main() {}")
    target = FlutterTargetBinding.from_dict(
        {
            "target_key": "target-android",
            "label": "Android device",
            "tags": ["mobile"],
            "roles": ["device"],
            "aliases": ["phone"],
            "preparation": [],
            "rank": [1],
            "binding": {"device": "private"},
            "platform": "android",
            "transport": "usb",
            "target_fingerprint": "fingerprint",
            "device_id": "private-device",
            "app_id": "com.example.app",
            "modes": ["debug"],
            "flavors": [None],
            "capabilities": {"hot_reload": True, "hot_restart": True},
            "android": {
                "adb": "/private/adb",
                "serial": "private-device",
                "transport": "usb",
                "identity": {
                    "ro_serialno": "serial",
                    "build_fingerprint": "fingerprint",
                    "product_device": "device",
                    "api_level": 35,
                    "avd_name": None,
                    "usb_transport": "1-1",
                },
            },
        }
    )
    owner = FlutterSessionOwner(
        FlutterLaunchConfig(
            "/private/flutter",
            target,
            FlutterOptions("lib/main.dart", None, "debug"),
            {},
        ),
        worktree=tmp_path,
        source="a" * 40,
    )
    assert not owner._record_machine(
        {
            "event": "app.start",
            "params": {
                "appId": "opaque-machine-id",
                "directory": str(tmp_path),
                "deviceId": "private-device",
                "launchMode": "attach",
                "mode": "debug",
            },
        }
    )
    assert not owner._record_machine(
        {
            "event": "app.start",
            "params": {
                "appId": "opaque-machine-id",
                "directory": str(tmp_path),
                "deviceId": "replacement-device",
                "launchMode": "run",
                "mode": "debug",
            },
        }
    )
    assert owner._record_machine(
        {
            "event": "app.start",
            "params": {
                "appId": "opaque-machine-id",
                "directory": str(tmp_path),
                "deviceId": "private-device",
                "launchMode": "run",
                "mode": "debug",
            },
        }
    )
    assert owner._record_machine(
        {"event": "app.started", "params": {"appId": "opaque-machine-id"}}
    )


def test_log_redaction_removes_control_urls_and_credentials() -> None:
    text = "connected http://user:token@127.0.0.1:8181/?auth=secret ws://localhost:9000/?token=x"
    safe = redact_log(text)
    assert "token" not in safe
    assert "127.0.0.1" not in safe
    assert "localhost" not in safe
    assert "connected" in safe
