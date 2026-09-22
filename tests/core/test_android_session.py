from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import threading
import xml.etree.ElementTree as ET

import pytest

from mship.core import android_adapter, android_session
from mship.core.android_session import (
    AndroidBinding,
    AndroidProfileOptions,
    AndroidSessionOwner,
    TargetEnvelope,
)
from mship.core.session_channel import OwnerContext
from mship.core.session_inputs import (
    CaptureGrant,
    InstallFromResult,
    InstallGrant,
    OwnerRequest,
    SessionError,
)


def _binding(
    *,
    transport: str = "emulator",
    avd_name: object = "Pixel_API_35",
    usb_transport: object = None,
) -> dict[str, object]:
    return {
        "adb": "/opt/android/adb",
        "serial": "emulator-5554",
        "transport": transport,
        "ro_serialno": "emu-private",
        "build_fingerprint": "vendor/device:35/test",
        "product_device": "device",
        "api_level": 35,
        "avd_name": avd_name,
        "usb_transport": usb_transport,
        "package": "com.example.app",
        "component": "com.example.app/.MainActivity",
        "package_inspector": "/opt/tools/apk-inspect",
        "capabilities": ["capture", "run"],
        "fixtures": {},
    }


@pytest.mark.parametrize(
    ("binding_component", "profile_component"),
    [
        (
            "com.example.app/com.example.app.MainActivity",
            "com.example.app/.MainActivity",
        ),
        (
            "com.example.app/.MainActivity",
            "com.example.app/com.example.app.MainActivity",
        ),
    ],
    ids=["full-binding-short-profile", "short-binding-full-profile"],
)
def test_profile_accepts_equivalent_component_spelling_without_rewriting(
    binding_component: str, profile_component: str
) -> None:
    value = _binding()
    value["component"] = binding_component
    binding = AndroidBinding.from_value(value)

    profile = AndroidProfileOptions.from_value(
        {
            "package": binding.package,
            "component": profile_component,
            "instrumentation": None,
        },
        binding,
    )

    assert binding.component == binding_component
    assert profile.component == profile_component


def test_profile_cannot_retarget_selected_application():
    binding = AndroidBinding.from_value(_binding())
    with pytest.raises(SessionError, match="does not match"):
        AndroidProfileOptions.from_value(
            {
                "package": "com.example.other",
                "component": "com.example.other/.MainActivity",
                "instrumentation": None,
            },
            binding,
        )


def test_capture_cancellation_removes_only_partial_capture_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):

    def fake_stream(
        _adb: str,
        _serial: str,
        _args: tuple[str, ...],
        destination: Path,
        _cancel: threading.Event,
    ) -> None:
        destination.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    def fake_adb(_adb: str, _serial: str, *_args: str, **_kwargs: object) -> bytes:
        raise SessionError("cancelled", "Android operation was cancelled")

    monkeypatch.setattr(android_adapter, "_stream_adb_file", fake_stream)
    monkeypatch.setattr(android_adapter, "_adb", fake_adb)
    with pytest.raises(SessionError, match="cancelled"):
        android_adapter.capture_android(
            "/opt/android/adb",
            "emulator-5554",
            tmp_path,
            ("image", "layout"),
            threading.Event(),
        )

    assert not (tmp_path / "screen.png").exists()


def test_layout_capture_returns_xml_without_uiautomator_status(tmp_path, monkeypatch):
    monkeypatch.setattr(
        android_adapter,
        "_adb",
        lambda *args, **kwargs: (
            b'<?xml version="1.0"?><hierarchy rotation="0"><node text="Home"/></hierarchy>'
            b"UI hierchary dumped to: /dev/tty\n"
        ),
    )
    android_adapter.capture_android(
        "/opt/android/adb", "emulator-5554", tmp_path, ("layout",), threading.Event()
    )
    root = ET.parse(tmp_path / "layout.xml").getroot()
    assert root.tag == "hierarchy"
    assert root.find("node").attrib["text"] == "Home"


def test_layout_capture_rejects_malformed_hierarchy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        android_adapter,
        "_adb",
        lambda *args, **kwargs: b"<hierarchy><node></hierarchy>",
    )
    with pytest.raises(SessionError) as error:
        android_adapter.capture_android(
            "/opt/android/adb",
            "emulator-5554",
            tmp_path,
            ("layout",),
            threading.Event(),
        )
    assert error.value.code == "unhealthy"
    assert not (tmp_path / "layout.xml").exists()


@pytest.mark.parametrize(
    "activity_dump,expected",
    [
        (
            "topResumedActivity=ActivityRecord{38202027 u0 com.example.app/.MainActivity t26}",
            "com.example.app",
        ),
        (
            "mResumedActivity: ActivityRecord{38202027 u0 com.example.app/.MainActivity t26}",
            "com.example.app",
        ),
        (
            "topResumedActivity=null\nActivityRecord{38202027 u0 com.example.app/.MainActivity t26}",
            None,
        ),
    ],
)
def test_foreground_identity_reads_resumed_activity_without_window_focus(
    monkeypatch, activity_dump, expected
):
    monkeypatch.setattr(
        android_adapter,
        "_adb",
        lambda *args, **kwargs: (
            activity_dump.encode() if args[-1] == "activities" else b""
        ),
    )
    assert (
        android_adapter.foreground_android("/opt/android/adb", "emulator-5554")
        == expected
    )


def test_emulator_probe_rejects_missing_avd_identity(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        android_adapter,
        "_device_line",
        lambda *_args: ("device", "emulator-5554 device product:test"),
    )

    properties = {
        "ro.serialno": b"serial",
        "ro.build.fingerprint": b"fingerprint",
        "ro.product.device": b"product",
        "ro.build.version.sdk": b"35",
    }
    monkeypatch.setattr(
        android_adapter, "_adb", lambda *args, **_kwargs: properties.get(args[-1], b"")
    )

    with pytest.raises(SessionError, match="emulator identity"):
        android_adapter.probe_android("/opt/android/adb", "emulator-5554", "emulator")


def _fake_adb(
    tmp_path: Path,
    *,
    launch_acknowledged: bool = True,
    capture_effect: str | None = None,
    race_reverse_create: bool = False,
    malformed_forward_listing: bool = False,
) -> tuple[Path, Path]:
    state_path = tmp_path / "adb-state.json"
    state_path.write_text(
        json.dumps(
            {
                "launch_acknowledged": launch_acknowledged,
                "capture_effect": capture_effect,
                "commands": [],
                "devices": {
                    "emulator-5554": {
                        "identity": {
                            "ro.serialno": "emu-private",
                            "ro.build.fingerprint": "vendor/device:35/test",
                            "ro.product.device": "device",
                            "ro.build.version.sdk": "35",
                            "ro.kernel.qemu.avd_name": "Pixel_API_35",
                        },
                        "foreground": "com.android.launcher",
                        "packages": {
                            "com.example.app": {"running": False, "pid": "111"},
                            "com.unrelated.app": {"running": True, "pid": "222"},
                        },
                    },
                    "emulator-5556": {
                        "identity": {
                            "ro.serialno": "other-private",
                            "ro.build.fingerprint": "vendor/other:35/test",
                            "ro.product.device": "other",
                            "ro.build.version.sdk": "35",
                            "ro.kernel.qemu.avd_name": "Other_API_35",
                        },
                        "foreground": "com.example.app",
                        "packages": {
                            "com.example.app": {"running": True, "pid": "333"}
                        },
                    },
                },
                "forwards": [],
                "reverses": {
                    "emulator-5554": [],
                    "emulator-5556": [],
                },
                "race_reverse_create": race_reverse_create,
                "malformed_forward_listing": malformed_forward_listing,
            }
        )
    )
    adb = tmp_path / "adb"
    adb.write_text("""#!/usr/bin/python3
import json
import sys
from pathlib import Path

state_path = Path(__file__).with_name("adb-state.json")
state = json.loads(state_path.read_text())
argv = sys.argv[1:]

if argv == ["devices", "-l"]:
    state["commands"].append({"serial": None, "command": argv})
    state_path.write_text(json.dumps(state))
    print("List of devices attached")
    print("emulator-5554 device product:device")
    print("emulator-5556 device product:other")
    raise SystemExit(0)

if len(argv) < 3 or argv[0] != "-s":
    raise SystemExit(2)
serial, command = argv[1], argv[2:]
state["commands"].append({"serial": serial, "command": command})
device = state["devices"].get(serial)
if device is None:
    state_path.write_text(json.dumps(state))
    raise SystemExit(2)

def focus():
    package = device["foreground"]
    if package is not None:
        print(f"mCurrentFocus=Window{{123 u0 {package}/.MainActivity}}")

if len(command) == 3 and command[:2] == ["shell", "getprop"]:
    print(device["identity"].get(command[2], ""))
elif command == ["shell", "dumpsys", "window", "windows"]:
    focus()
elif command == ["shell", "dumpsys", "activity", "activities"]:
    focus()
elif command == ["shell", "am", "start", "-W", "-n", "com.example.app/.MainActivity"]:
    device["packages"]["com.example.app"]["running"] = True
    device["foreground"] = "com.example.app"
    if state["launch_acknowledged"]:
        print("Status: ok")
    else:
        print("Error: launch acknowledgement lost")
elif command == ["shell", "am", "force-stop", "com.example.app"]:
    device["packages"]["com.example.app"]["running"] = False
    if device["foreground"] == "com.example.app":
        device["foreground"] = "com.android.launcher"
elif command == ["shell", "pidof", "com.example.app"]:
    if state.get("pidof_error"):
        state_path.write_text(json.dumps(state))
        print("error: device disconnected", file=sys.stderr)
        raise SystemExit(1)
    package = device["packages"]["com.example.app"]
    if package["running"]:
        print(package["pid"])
    else:
        state_path.write_text(json.dumps(state))
        raise SystemExit(1)
elif command[:3] == ["shell", "am", "instrument"]:
    print(state["instrumentation_report"])
elif command == ["forward", "--list"]:
    if state["malformed_forward_listing"]:
        print("malformed")
    else:
        for mapping in state["forwards"]:
            print(mapping["serial"], mapping["local"], mapping["remote"])
elif len(command) == 4 and command[:2] == ["forward", "--no-rebind"]:
    local, remote = command[2:]
    if any(mapping["local"] == local for mapping in state["forwards"]):
        state_path.write_text(json.dumps(state))
        print("cannot rebind", file=sys.stderr)
        raise SystemExit(1)
    state["forwards"].append({"serial": serial, "local": local, "remote": remote})
elif len(command) == 3 and command[:2] == ["forward", "--remove"]:
    local = command[2]
    state["forwards"] = [
        mapping
        for mapping in state["forwards"]
        if not (mapping["serial"] == serial and mapping["local"] == local)
    ]
elif command == ["reverse", "--list"]:
    for mapping in state["reverses"][serial]:
        print("host", mapping["remote"], mapping["local"])
elif len(command) == 4 and command[:2] == ["reverse", "--no-rebind"]:
    remote, local = command[2:]
    mappings = state["reverses"][serial]
    if state["race_reverse_create"]:
        mappings.append({"local": "tcp:49999", "remote": remote})
        state["race_reverse_create"] = False
    if any(mapping["remote"] == remote for mapping in mappings):
        state_path.write_text(json.dumps(state))
        print("cannot rebind", file=sys.stderr)
        raise SystemExit(1)
    mappings.append({"local": local, "remote": remote})
elif len(command) == 3 and command[:2] == ["reverse", "--remove"]:
    remote = command[2]
    state["reverses"][serial] = [
        mapping
        for mapping in state["reverses"][serial]
        if mapping["remote"] != remote
    ]
elif command == ["exec-out", "screencap", "-p"]:
    if state["capture_effect"] == "changed":
        device["foreground"] = "com.unrelated.app"
    elif state["capture_effect"] == "unknown":
        device["foreground"] = None
    state_path.write_text(json.dumps(state))
    sys.stdout.buffer.write(b"\\x89PNG\\r\\n\\x1a\\nimage")
    raise SystemExit(0)

state_path.write_text(json.dumps(state))
""")
    adb.chmod(0o755)
    return adb, state_path


def _owner(
    tmp_path: Path,
    adb: Path,
    *,
    instrumentation: str | None = None,
    fixtures: dict[str, object] | None = None,
    component: str | None = None,
    install: bool = False,
) -> tuple[AndroidSessionOwner, OwnerContext]:
    private_root = tmp_path / "owner"
    private_root.mkdir(mode=0o700, exist_ok=True)
    context = OwnerContext(
        task="task",
        repo="repo",
        owner_ref="a" * 24,
        generation="b" * 24,
        source_revision="c" * 40,
        workspace_root=tmp_path,
        worktree=tmp_path,
        private_root=private_root,
        socket_path=tmp_path / "owner.sock",
        secret="d" * 24,
    )
    value = _binding()
    if component is not None:
        value["component"] = component
    capabilities = ["launch", "capture"] + (["instrument"] if instrumentation else [])
    task_keys = {
        "launch": "launch-task",
        "capture": "capture-task",
        "instrument": "instrument-task",
    }
    if install:
        capabilities.append("install")
        task_keys["install"] = "install-task"
    if fixtures is not None:
        capabilities.append("fixture")
        task_keys["fixture"] = "fixture-task"
    value.update(
        {
            "adb": str(adb),
            "package_inspector": str(adb),
            "capabilities": capabilities,
            "fixtures": fixtures or {},
        }
    )
    binding = AndroidBinding.from_value(value)
    target = TargetEnvelope(
        "android-run",
        "launch",
        binding.capabilities,
        task_keys,
        binding,
        AndroidProfileOptions.from_value(
            {
                "package": binding.package,
                "component": binding.component,
                "instrumentation": instrumentation,
            },
            binding,
        ),
    )
    return AndroidSessionOwner(context, target), context


def _request(operation: str, *, capture: CaptureGrant | None = None) -> OwnerRequest:
    return OwnerRequest(
        operation_ref="e" * 24,
        operation=operation,
        source_revision="c" * 40,
        expires_at=4_102_444_800.0,
        capture=capture,
    )


def _install_request(path: Path) -> OwnerRequest:
    result = InstallFromResult(
        "r" * 24, "a" * 24, sha256(path.read_bytes()).hexdigest()
    )
    return OwnerRequest(
        operation_ref="e" * 24,
        operation="install",
        source_revision="c" * 40,
        expires_at=4_102_444_800.0,
        install=InstallGrant(result, path, path.stat().st_size),
    )


@pytest.mark.parametrize(
    ("configured_component", "inspected_component"),
    [
        (
            "com.example.app/com.example.app.MainActivity",
            "com.example.app/.MainActivity",
        ),
        (
            "com.example.app/.MainActivity",
            "com.example.app/com.example.app.MainActivity",
        ),
    ],
    ids=["full-configured-short-inspected", "short-configured-full-inspected"],
)
def test_install_accepts_equivalent_component_spelling_from_inspected_apk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_component: str,
    inspected_component: str,
) -> None:
    adb, _ = _fake_adb(tmp_path)
    owner, _ = _owner(tmp_path, adb, component=configured_component, install=True)
    artifact = tmp_path / "app.apk"
    artifact.write_bytes(b"apk")
    monkeypatch.setattr(
        android_session,
        "_run",
        lambda _argv, **_kwargs: json.dumps(
            {"package": "com.example.app", "component": inspected_component}
        ).encode(),
    )

    def adb_reply(_adb: str, _serial: str, *args: str, **_kwargs: object) -> bytes:
        if args[0] == "install":
            return b"Success"
        if args[:5] == (
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
        ):
            return configured_component.encode()
        raise AssertionError(args)

    monkeypatch.setattr(android_session, "_adb", adb_reply)
    try:
        result = owner.handle(
            _install_request(artifact), {}, threading.Event(), lambda _event: None
        )
        assert result["installation"]["known"] is True
    finally:
        owner.shutdown()


def test_install_accepts_equivalent_component_spelling_from_postinstall_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_component = "com.example.app/com.example.app.MainActivity"
    acknowledged_component = "com.example.app/.MainActivity"
    adb, _ = _fake_adb(tmp_path)
    owner, _ = _owner(tmp_path, adb, component=configured_component, install=True)
    artifact = tmp_path / "app.apk"
    artifact.write_bytes(b"apk")
    monkeypatch.setattr(
        android_session,
        "_run",
        lambda _argv, **_kwargs: json.dumps(
            {"package": "com.example.app", "component": configured_component}
        ).encode(),
    )

    def adb_reply(_adb: str, _serial: str, *args: str, **_kwargs: object) -> bytes:
        if args[0] == "install":
            return b"Success"
        if args[:5] == (
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
        ):
            return acknowledged_component.encode()
        raise AssertionError(args)

    monkeypatch.setattr(android_session, "_adb", adb_reply)
    try:
        result = owner.handle(
            _install_request(artifact), {}, threading.Event(), lambda _event: None
        )
        assert result["installation"]["known"] is True
    finally:
        owner.shutdown()


def test_modern_emulator_owner_rejects_changed_boot_avd_identity(tmp_path):
    adb, state_path = _fake_adb(tmp_path)
    state = json.loads(state_path.read_text())
    identity = state["devices"]["emulator-5554"]["identity"]
    identity["ro.boot.qemu.avd_name"] = identity.pop("ro.kernel.qemu.avd_name")
    state_path.write_text(json.dumps(state))
    owner, _ = _owner(tmp_path, adb)
    try:
        result = owner.handle(_request("launch"), {}, threading.Event(), lambda _: None)
        assert result["state"] == "active"
        state = json.loads(state_path.read_text())
        state["devices"]["emulator-5554"]["identity"]["ro.boot.qemu.avd_name"] = (
            "Replaced"
        )
        state_path.write_text(json.dumps(state))
        assert owner._status()["state"] == "unknown"
        with pytest.raises(SessionError, match="identity changed"):
            owner.handle(_request("launch"), {}, threading.Event(), lambda _: None)
    finally:
        state = json.loads(state_path.read_text())
        state["devices"]["emulator-5554"]["identity"]["ro.boot.qemu.avd_name"] = (
            "Pixel_API_35"
        )
        state_path.write_text(json.dumps(state))
        assert owner.shutdown()


def test_second_app_owner_cannot_stop_first_and_can_launch_after_release(tmp_path):
    adb, state_path = _fake_adb(tmp_path)
    owners = []
    for name in ("a", "b", "c"):
        directory = tmp_path / name
        directory.mkdir()
        owners.append(_owner(directory, adb)[0])
    first, second, successor = owners
    try:
        first.handle(_request("launch"), {}, threading.Event(), lambda _: None)
        with pytest.raises(SessionError) as rejected:
            second.handle(_request("launch"), {}, threading.Event(), lambda _: None)
        assert rejected.value.code == "busy"
        assert second.shutdown()
        state = json.loads(state_path.read_text())
        assert state["devices"]["emulator-5554"]["packages"]["com.example.app"][
            "running"
        ]
        assert first.shutdown()
        successor.handle(_request("launch"), {}, threading.Event(), lambda _: None)
        state = json.loads(state_path.read_text())
        assert state["devices"]["emulator-5554"]["packages"]["com.example.app"][
            "running"
        ]
    finally:
        for owner in owners:
            owner.shutdown()


def _fixture_config(kind: str) -> dict[str, object]:
    return {
        "bridge": {
            "kind": kind,
            "local": "tcp:41001",
            "remote": "tcp:42001",
        }
    }


@pytest.mark.parametrize("kind", ["forward", "reverse"])
def test_fixture_cleanup_removes_owned_exact_mapping_from_recovered_journal(
    tmp_path: Path, kind: str
) -> None:
    adb, state_path = _fake_adb(tmp_path)
    fixtures = _fixture_config(kind)
    owner, _ = _owner(tmp_path, adb, fixtures=fixtures)
    owner.handle(
        _request("fixture"),
        {"fixture_id": "bridge"},
        threading.Event(),
        lambda _event: None,
    )

    restored, _ = _owner(tmp_path, adb, fixtures=fixtures)
    assert restored.shutdown() is True

    state = json.loads(state_path.read_text())
    if kind == "forward":
        assert state["forwards"] == []
    else:
        assert state["reverses"]["emulator-5554"] == []


@pytest.mark.parametrize("kind", ["forward", "reverse"])
def test_missing_owned_fixture_mapping_acknowledges_cleanup(
    tmp_path: Path, kind: str
) -> None:
    adb, state_path = _fake_adb(tmp_path)
    fixtures = _fixture_config(kind)
    owner, _ = _owner(tmp_path, adb, fixtures=fixtures)
    owner.handle(
        _request("fixture"),
        {"fixture_id": "bridge"},
        threading.Event(),
        lambda _event: None,
    )
    state = json.loads(state_path.read_text())
    if kind == "forward":
        state["forwards"] = []
    else:
        state["reverses"]["emulator-5554"] = []
    state_path.write_text(json.dumps(state))

    restored, _ = _owner(tmp_path, adb, fixtures=fixtures)
    assert restored.shutdown() is True


def test_fixture_rejection_and_cleanup_preserve_another_devices_forward_mapping(
    tmp_path: Path,
) -> None:
    adb, state_path = _fake_adb(tmp_path)
    fixtures = _fixture_config("forward")
    state = json.loads(state_path.read_text())
    existing = {
        "serial": "emulator-5556",
        "local": "tcp:41001",
        "remote": "tcp:49999",
    }
    state["forwards"].append(existing)
    state_path.write_text(json.dumps(state))
    owner, _ = _owner(tmp_path, adb, fixtures=fixtures)

    with pytest.raises(SessionError):
        owner.handle(
            _request("fixture"),
            {"fixture_id": "bridge"},
            threading.Event(),
            lambda _event: None,
        )
    assert owner.shutdown() is True

    state = json.loads(state_path.read_text())
    assert state["forwards"] == [existing]


def test_fixture_cleanup_refuses_a_malformed_mapping_listing_without_deletion(
    tmp_path: Path,
) -> None:
    adb, state_path = _fake_adb(tmp_path)
    fixtures = _fixture_config("forward")
    owner, _ = _owner(tmp_path, adb, fixtures=fixtures)
    owner.handle(
        _request("fixture"),
        {"fixture_id": "bridge"},
        threading.Event(),
        lambda _event: None,
    )
    state = json.loads(state_path.read_text())
    mapping = state["forwards"][0]
    state["malformed_forward_listing"] = True
    state_path.write_text(json.dumps(state))

    restored, _ = _owner(tmp_path, adb, fixtures=fixtures)
    assert restored.shutdown() is False

    state = json.loads(state_path.read_text())
    assert state["forwards"] == [mapping]


def test_fixture_cleanup_refuses_an_ambiguous_mapping_without_deletion(
    tmp_path: Path,
) -> None:
    adb, state_path = _fake_adb(tmp_path)
    fixtures = _fixture_config("forward")
    owner, _ = _owner(tmp_path, adb, fixtures=fixtures)
    owner.handle(
        _request("fixture"),
        {"fixture_id": "bridge"},
        threading.Event(),
        lambda _event: None,
    )
    state = json.loads(state_path.read_text())
    mappings = state["forwards"]
    mappings.append(
        {
            "serial": "emulator-5554",
            "local": "tcp:41001",
            "remote": "tcp:49999",
        }
    )
    state_path.write_text(json.dumps(state))

    restored, _ = _owner(tmp_path, adb, fixtures=fixtures)
    assert restored.shutdown() is False

    state = json.loads(state_path.read_text())
    assert state["forwards"] == mappings


@pytest.mark.parametrize("kind", ["forward", "reverse"])
def test_replaced_fixture_mapping_is_preserved_and_cleanup_is_unknown(
    tmp_path: Path, kind: str
) -> None:
    adb, state_path = _fake_adb(tmp_path)
    fixtures = _fixture_config(kind)
    owner, _ = _owner(tmp_path, adb, fixtures=fixtures)
    owner.handle(
        _request("fixture"),
        {"fixture_id": "bridge"},
        threading.Event(),
        lambda _event: None,
    )
    state = json.loads(state_path.read_text())
    if kind == "forward":
        replacement = {
            "serial": "emulator-5554",
            "local": "tcp:41001",
            "remote": "tcp:49999",
        }
        state["forwards"] = [replacement]
    else:
        replacement = {"local": "tcp:49999", "remote": "tcp:42001"}
        state["reverses"]["emulator-5554"] = [replacement]
    state_path.write_text(json.dumps(state))

    restored, _ = _owner(tmp_path, adb, fixtures=fixtures)
    assert restored.shutdown() is False

    state = json.loads(state_path.read_text())
    if kind == "forward":
        assert state["forwards"] == [replacement]
    else:
        assert state["reverses"]["emulator-5554"] == [replacement]


def test_reverse_no_rebind_collision_never_adopts_or_removes_racing_mapping(
    tmp_path: Path,
) -> None:
    adb, state_path = _fake_adb(tmp_path, race_reverse_create=True)
    fixtures = _fixture_config("reverse")
    owner, _ = _owner(tmp_path, adb, fixtures=fixtures)

    with pytest.raises(SessionError):
        owner.handle(
            _request("fixture"),
            {"fixture_id": "bridge"},
            threading.Event(),
            lambda _event: None,
        )

    restored, _ = _owner(tmp_path, adb, fixtures=fixtures)
    assert restored.shutdown() is False
    state = json.loads(state_path.read_text())
    assert state["reverses"]["emulator-5554"] == [
        {"local": "tcp:49999", "remote": "tcp:42001"}
    ]


def test_lost_launch_acknowledgement_persists_exact_package_cleanup_intent(
    tmp_path: Path,
) -> None:
    adb, state_path = _fake_adb(tmp_path, launch_acknowledged=False)
    owner, _ = _owner(tmp_path, adb)

    try:
        with pytest.raises(SessionError, match="launch was not acknowledged"):
            owner.handle(_request("launch"), {}, threading.Event(), lambda _event: None)
        restored, _ = _owner(tmp_path, adb)
        assert restored.shutdown() is False
        state = json.loads(state_path.read_text())
        assert state["devices"]["emulator-5554"]["packages"]["com.example.app"][
            "running"
        ]
        assert owner.shutdown()
    finally:
        owner.shutdown()
    state = json.loads(state_path.read_text())
    target = state["devices"]["emulator-5554"]
    assert target["packages"]["com.example.app"]["running"] is False
    assert target["packages"]["com.unrelated.app"]["running"] is True
    assert (
        state["devices"]["emulator-5556"]["packages"]["com.example.app"]["running"]
        is True
    )


@pytest.mark.parametrize(
    ("capture_effect", "foreground_before_capture", "message"),
    [
        ("changed", None, "capture did not preserve selected app identity"),
        ("unknown", None, "capture did not preserve selected app identity"),
        (None, "com.unrelated.app", "Selected Android app is not foreground"),
    ],
    ids=["foreground-changed", "foreground-unknown", "foreground-stale"],
)
def test_capture_rejects_stale_changed_or_unknown_foreground_without_another_device_fallback(
    tmp_path: Path,
    capture_effect: str | None,
    foreground_before_capture: str | None,
    message: str,
) -> None:
    adb, state_path = _fake_adb(tmp_path, capture_effect=capture_effect)
    owner, _ = _owner(tmp_path, adb)
    try:
        owner.handle(_request("launch"), {}, threading.Event(), lambda _event: None)
        if foreground_before_capture is not None:
            state = json.loads(state_path.read_text())
            state["devices"]["emulator-5554"]["foreground"] = foreground_before_capture
            state_path.write_text(json.dumps(state))
        directory = tmp_path / "capture"
        directory.mkdir()

        with pytest.raises(SessionError, match=message):
            owner.handle(
                _request(
                    "capture", capture=CaptureGrant(directory, ("image",), "android")
                ),
                {},
                threading.Event(),
                lambda _event: None,
            )

        state = json.loads(state_path.read_text())
        assert state["devices"]["emulator-5556"]["foreground"] == "com.example.app"
        assert all(
            command["serial"] in {None, "emulator-5554"}
            for command in state["commands"]
        )
        if foreground_before_capture is not None:
            assert not any(
                command["command"] == ["exec-out", "screencap", "-p"]
                for command in state["commands"]
            )
    finally:
        owner.shutdown()


def test_adb_error_cannot_acknowledge_cleanup_as_an_absent_process(
    tmp_path: Path,
) -> None:
    adb, state_path = _fake_adb(tmp_path)
    owner, _ = _owner(tmp_path, adb)
    owner.handle(_request("launch"), {}, threading.Event(), lambda _event: None)
    state = json.loads(state_path.read_text())
    state["pidof_error"] = True
    state_path.write_text(json.dumps(state))
    assert owner.shutdown() is False

    state = json.loads(state_path.read_text())
    state["pidof_error"] = False
    state_path.write_text(json.dumps(state))
    restored, _ = _owner(tmp_path, adb)
    assert restored.shutdown() is True
    state = json.loads(state_path.read_text())
    assert (
        state["devices"]["emulator-5556"]["packages"]["com.example.app"]["running"]
        is True
    )


@pytest.mark.parametrize(
    "report",
    [
        "INSTRUMENTATION_STATUS_CODE: 0\nINSTRUMENTATION_CODE: 0",
        "INSTRUMENTATION_STATUS_CODE: -2\nINSTRUMENTATION_CODE: -1",
        "INSTRUMENTATION_STATUS_CODE: invalid\nINSTRUMENTATION_CODE: -1",
    ],
)
def test_instrumentation_rejects_runner_cancellation_failed_tests_and_invalid_status(
    tmp_path: Path, report: str
) -> None:
    adb, state_path = _fake_adb(tmp_path)
    state = json.loads(state_path.read_text())
    state["instrumentation_report"] = report
    state_path.write_text(json.dumps(state))
    owner, _ = _owner(tmp_path, adb, instrumentation="com.example.app/.Runner")
    with pytest.raises(SessionError, match="instrumentation did not report success"):
        owner.handle(_request("instrument"), {}, threading.Event(), lambda _event: None)


def test_discovered_binding_launches_exact_app_and_rejects_changed_host_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adb, state_path = _fake_adb(tmp_path)
    _, context = _owner(tmp_path, adb)
    binding = _binding()
    binding.update(
        adb=str(adb), package_inspector=str(adb), capabilities=["launch", "capture"]
    )
    local = {"paths": {"android": {"bindings": {"phone": binding}}}, "aliases": {}}
    request = {
        "protocol_version": 1,
        "task": "task",
        "repo": "repo",
        "backend": "native",
        "backend_revision": context.source_revision,
        "profile": "phone",
        "profile_revision": "f" * 64,
        "operation": "launch",
        "target_alias": None,
        "options": {
            "package": binding["package"],
            "component": binding["component"],
            "instrumentation": None,
        },
    }

    def private_input(name: str, value: object) -> None:
        path = context.private_root / f"{name}.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        monkeypatch.setenv(name, str(path))

    private_input("MSHIP_TARGET_REQUEST_FILE", request)
    private_input("MSHIP_TARGET_BINDINGS_FILE", local)
    android_session.discover_main()
    candidate = json.loads(capsys.readouterr().out)["candidates"][0]
    private_input(
        "MSHIP_TARGET_CONTEXT_FILE",
        {
            "protocol_version": 1,
            "run_id": "android-run",
            "task": "task",
            "repo": "repo",
            "profile": "phone",
            "profile_revision": "f" * 64,
            "backend": "native",
            "backend_revision": context.source_revision,
            "host_name": "fixture",
            "host_scope": "user",
            "host_endpoint_fingerprint": "f" * 64,
            "operation": "launch",
            "capabilities": candidate["capabilities"],
            "private_binding": candidate["binding"],
            "session_owner": "android",
            "task_keys": {"launch": "launch-task", "capture": "capture-task"},
        },
    )
    owner = AndroidSessionOwner.from_environ(context)
    try:
        owner.handle(_request("launch"), {}, threading.Event(), lambda _event: None)
        state = json.loads(state_path.read_text())
        assert (
            state["devices"]["emulator-5554"]["packages"]["com.example.app"]["running"]
            is True
        )
        binding["ro_serialno"] = "changed-target"
        private_input("MSHIP_TARGET_BINDINGS_FILE", local)
        with pytest.raises(SessionError, match="binding no longer matches"):
            AndroidSessionOwner.from_environ(context)
    finally:
        assert owner.shutdown() is True
