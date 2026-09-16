"""Behavioral contracts for dynamic Android and Flutter target examples."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mship.backends.android import backend as android_backend
from mship.backends.flutter import backend as flutter_backend

_FIXTURES = Path(__file__).with_name("fixtures")


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
    backend = android_backend
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


@pytest.mark.parametrize(
    "avd_property", ["ro.boot.qemu.avd_name", "ro.kernel.qemu.avd_name"]
)
def test_android_discovery_accepts_modern_and_legacy_avd_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    avd_property: str,
):
    backend = android_backend
    fixture = json.loads(
        (_FIXTURES / "mobile-android-devices-redacted.json").read_text()
    )
    properties = fixture["properties"]["emulator-5554"]
    properties[avd_property] = properties.pop("ro.kernel.qemu.avd_name")
    template = _android_template()
    request = _request()
    request["options"] = {
        "package": template["package"],
        "component": template["component"],
        "instrumentation": None,
        "platform": "android",
        "transport": "emulator",
    }
    monkeypatch.setattr(backend, "load_request", lambda: request)
    monkeypatch.setattr(
        backend,
        "load_bindings",
        lambda: {"paths": {"android": template}, "aliases": {}},
    )
    monkeypatch.setattr(
        backend, "_adb_service", lambda _: b"emulator-5554 device product:device"
    )

    def shell(_serial, command, *, getprop=True):
        if getprop:
            return properties.get(command, "")
        if command.startswith("cmd package path "):
            return "package:/data/app/base.apk"
        if command.startswith("cmd package resolve-activity "):
            return template["component"]
        raise AssertionError(command)

    monkeypatch.setattr(backend, "_adb_shell", shell)
    backend.discover()
    (candidate,) = json.loads(capsys.readouterr().out)["candidates"]
    assert candidate["ready"]
    assert candidate["binding"]["android"]["avd_name"] == "Pixel_9_API_35"


def test_flutter_fixture_classifies_android_and_preserves_platform_in_private_descriptor():
    backend = flutter_backend
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
    backend = flutter_backend
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
    backend = flutter_backend
    monkeypatch.setattr(backend, "load_request", _request)
    monkeypatch.setattr(
        backend, "load_context", lambda: {"private_binding": {"target_key": "lost"}}
    )

    with pytest.raises(backend.ExampleError, match="explicit platform"):
        backend._owner_bindings()


def test_flutter_discovery_reports_unavailable_binding_without_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    backend = flutter_backend
    monkeypatch.setattr(backend, "load_request", _request)
    monkeypatch.setattr(backend, "load_bindings", lambda: {"paths": {}})

    backend.discover()

    inventory = json.loads(capsys.readouterr().out)
    assert inventory["errors"][0]["code"] == "flutter_unavailable"


def test_flutter_ios_discovery_and_probe_work_with_large_sdk_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    backend = flutter_backend
    device_id = "AAAAAAAA-1111-2222-3333-AAAAAAAAAAAA"
    data_path = tmp_path / "simulator"
    data_path.mkdir()
    inventory = {
        "devices": {
            "com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
                {
                    "udid": device_id,
                    "isAvailable": True,
                    "state": "Booted",
                    "dataPath": str(data_path),
                    "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
                }
            ]
        }
    }
    sdk_data = tmp_path / "devices.json"
    sdk_data.write_text(json.dumps(inventory))
    xcrun = tmp_path / "xcrun"
    xcrun.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"data = json.load(open({str(sdk_data)!r}))\n"
        "if 'devices' not in sys.argv:\n"
        "    data['devicetypes'] = ['unrelated device metadata' * 10000]\n"
        "print(json.dumps(data))\n"
    )
    xcrun.chmod(0o700)
    template = {
        "executable": sys.executable,
        "app_id": "com.example.product",
        "modes": ["debug"],
        "flavors": [None],
        "ios": {"xcrun": str(xcrun)},
    }
    monkeypatch.setattr(backend, "load_request", _request)
    monkeypatch.setattr(
        backend, "load_bindings", lambda: {"paths": {"flutter": template}}
    )

    backend.discover()

    candidate = json.loads(capsys.readouterr().out)["candidates"][0]
    assert candidate["ready"] is True
    assert candidate["rank"] == [1, 26, 5, 0]
    descriptor = candidate["binding"]
    probe = descriptor["ios"]["probe_argv"]
    # The child must retain the installed environment, not resolve out of its venv.
    receipt = json.loads(subprocess.check_output(probe, timeout=10))
    assert receipt["target_fingerprint"] == descriptor["target_fingerprint"]
    inventory["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-5"][0][
        "deviceTypeIdentifier"
    ] = "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro"
    sdk_data.write_text(json.dumps(inventory))
    changed = json.loads(subprocess.check_output(probe, timeout=10))
    assert changed["target_fingerprint"] != descriptor["target_fingerprint"]
    inventory["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-26-5"][0]["state"] = (
        "Shutdown"
    )
    sdk_data.write_text(json.dumps(inventory))
    backend.discover()
    stopped = json.loads(capsys.readouterr().out)["candidates"][0]
    assert stopped["ready"] is False
    assert stopped["capabilities"] == []
    refused = subprocess.run(probe, capture_output=True, timeout=10)
    assert refused.returncode != 0
