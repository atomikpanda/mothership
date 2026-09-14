"""Behavioral contracts for dynamic Android and Flutter target examples."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = Path(__file__).with_name("fixtures")
_ANDROID = _ROOT / "examples" / "run-targets" / "android-cli" / "backend.py"
_FLUTTER = _ROOT / "examples" / "run-targets" / "flutter" / "backend.py"


def _module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _android_template() -> dict[str, object]:
    return {
        "adb": "/bin/true",
        "package": "com.example.product",
        "component": "com.example.product/.MainActivity",
        "package_inspector": "/bin/true",
        "capabilities": ["run", "logs", "capture"],
        "fixtures": {},
    }


def _request() -> dict[str, object]:
    return {
        "protocol_version": 1,
        "backend": "mobile-example",
        "backend_revision": "a" * 40,
        "profile": "mobile",
        "profile_revision": "b" * 64,
        "task": "task",
        "repo": "app",
        "operation": "run",
        "options": {
            "entrypoint": "lib/main.dart",
            "flavor": None,
            "mode": "debug",
        },
        "target_alias": None,
    }


def test_android_fixture_distinguishes_usb_emulator_and_unauthorized_without_adb_child(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _module("android_mobile_example", _ANDROID)
    fixture = json.loads(
        (_FIXTURES / "mobile-android-devices-redacted.json").read_text()
    )
    monkeypatch.setattr(
        backend, "_adb_service", lambda _command: fixture["devices_l"].encode()
    )
    devices = backend._devices()

    assert backend._transport("R58MREDACTED", devices["R58MREDACTED"][1]) == "usb"
    assert (
        backend._transport("emulator-5554", devices["emulator-5554"][1]) == "emulator"
    )
    assert devices["R58UNAUTHORIZED"][0] == "unauthorized"


def test_android_dynamic_receipt_ranks_api_and_keeps_serial_only_in_binding(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _module("android_mobile_receipt", _ANDROID)
    fixture = json.loads(
        (_FIXTURES / "mobile-android-devices-redacted.json").read_text()
    )
    properties = fixture["properties"]["R58MREDACTED"]
    monkeypatch.setattr(
        backend, "_adb_shell", lambda _serial, property_name: properties[property_name]
    )

    raw = backend._dynamic_binding(
        _android_template(), "R58MREDACTED", fixture["devices_l"].splitlines()[0]
    )

    assert raw["transport"] == "usb"
    assert raw["api_level"] == 35
    assert raw["serial"] == "R58MREDACTED"
    assert backend._target_key(raw["serial"]).startswith("android-")


def test_flutter_fixture_classifies_android_and_preserves_platform_in_private_descriptor():
    backend = _module("flutter_mobile_example", _FLUTTER)
    devices = json.loads(
        (_FIXTURES / "mobile-flutter-devices-redacted.json").read_text()
    )
    android = {
        **devices[0],
        "android_identity": {
            "serial": "R58MREDACTED",
            "transport": "usb",
            "ro_serialno": "redacted",
            "build_fingerprint": "a" * 64,
            "product_device": "oriole",
            "api_level": 35,
            "avd_name": None,
            "usb_transport": "1-2",
        },
    }
    template = {
        "executable": "/bin/true",
        "app_id": "com.example.product",
        "modes": ["debug"],
        "flavors": [None],
        "android": {"adb": "/bin/true"},
    }

    candidate = backend._candidate(template, {"aliases": {}}, android, _request())

    assert candidate["tags"] == ["flutter", "android", "usb"]
    assert candidate["rank"] == [1, 35, 0, 0]
    assert candidate["binding"]["platform"] == "android"
    assert candidate["capabilities"] == ["run", "logs", "capture", "reload", "restart"]


def test_flutter_ios_simulator_runs_without_capture_attestation(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _module("flutter_mobile_ios", _FLUTTER)
    fixture = json.loads(
        (_FIXTURES / "mobile-flutter-devices-redacted.json").read_text()
    )
    simulator, device = fixture[2], fixture[3]
    assert backend._transport(simulator) == "simulator"
    assert backend._transport(device) == "usb"
    monkeypatch.setattr(
        backend, "_ios_inventory", lambda _tool, _id: ("a" * 64, (18, 2, 0))
    )
    template = {
        "executable": "/bin/true",
        "app_id": "com.example.product",
        "modes": ["debug"],
        "flavors": [None],
        "android": {"adb": "/bin/true"},
        "ios": {"xcrun": "/bin/true"},
    }

    candidate = backend._candidate(template, {"aliases": {}}, simulator, _request())

    assert candidate["capabilities"] == ["run", "logs", "reload", "restart"]
    assert candidate["binding"]["ios"]["foreground_argv"] is None
    assert candidate["binding"]["ios"]["capture_argv"] is None
    with pytest.raises(backend.SessionError, match="physical iOS"):
        backend._candidate(template, {"aliases": {}}, device, _request())


def test_flutter_owner_materialization_refuses_context_without_platform(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = _module("flutter_mobile_context", _FLUTTER)
    monkeypatch.setattr(backend, "load_request", _request)
    monkeypatch.setattr(
        backend, "load_context", lambda: {"private_binding": {"target_key": "lost"}}
    )

    with pytest.raises(backend.ExampleError, match="explicit platform"):
        backend._owner_bindings()
