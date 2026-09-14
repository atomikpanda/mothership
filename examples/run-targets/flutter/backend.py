"""Dynamic Flutter target inventory and exact-owner materialization."""

from __future__ import annotations

import importlib.util
from hashlib import sha256
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import (
    ExampleError,
    emit_inventory,
    load_bindings,
    load_context,
    load_request,
)
from mship.core.flutter_adapter import FlutterOptions, FlutterTargetBinding
from mship.core.flutter_session import main as owner_main
from mship.core.session_inputs import SessionError

_MAX_OUTPUT = 64 * 1024
_TEMPLATE_FIELDS = {"executable", "app_id", "modes", "flavors"}
_ANDROID_FIELDS = {"adb"}
_IOS_FIELDS = {"xcrun"}


def _run(argv: tuple[str, ...]) -> bytes:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("MSHIP_")
            },
        )
    except OSError as error:
        raise SessionError(
            "unavailable", "configured Flutter tool is unavailable"
        ) from error
    output = bytearray()
    started = time.monotonic()
    try:
        assert process.stdout is not None
        while process.poll() is None:
            if time.monotonic() - started > 10:
                raise SessionError("unavailable", "Flutter inventory did not respond")
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(process.stdout.fileno(), 8192)
            if chunk:
                output.extend(chunk)
                if len(output) > _MAX_OUTPUT:
                    raise SessionError(
                        "unavailable", "Flutter inventory response is too large"
                    )
        while True:
            chunk = os.read(process.stdout.fileno(), 8192)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > _MAX_OUTPUT:
                raise SessionError(
                    "unavailable", "Flutter inventory response is too large"
                )
        if process.wait(timeout=1) != 0:
            raise SessionError("unavailable", "Flutter inventory is unavailable")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


def _template(bindings: Mapping[str, object]) -> dict[str, object]:
    paths = bindings.get("paths")
    value = paths.get("flutter") if isinstance(paths, dict) else None
    allowed = {
        _TEMPLATE_FIELDS | {"android"},
        _TEMPLATE_FIELDS | {"ios"},
        _TEMPLATE_FIELDS | {"android", "ios"},
    }
    if not isinstance(value, dict) or set(value) not in allowed:
        raise ExampleError("Flutter host bindings are unavailable")
    executable = value["executable"]
    if (
        not isinstance(executable, str)
        or not os.path.isabs(executable)
        or not os.access(executable, os.X_OK)
    ):
        raise ExampleError("configured Flutter tool is unavailable")
    return value


def _device_key(platform: str, device_id: str) -> str:
    if (
        platform not in {"android", "ios"}
        or not isinstance(device_id, str)
        or not device_id
    ):
        raise SessionError("identity-unknown", "Flutter device identity is unavailable")
    return f"flutter-{platform}-" + sha256(device_id.encode("utf-8")).hexdigest()[:32]


def _transport(device: Mapping[str, object]) -> str | None:
    platform, emulator = device.get("targetPlatform"), device.get("emulator")
    if platform not in {"android", "ios"} or type(emulator) is not bool:
        return None
    if platform == "android":
        return "emulator" if emulator else "usb"
    return "simulator" if emulator else "usb"


def _runtime_rank(
    device: Mapping[str, object], platform: str, identity: Mapping[str, object] | None
) -> tuple[int, int, int]:
    if (
        platform == "android"
        and identity is not None
        and type(identity.get("api_level")) is int
    ):
        return (int(identity["api_level"]), 0, 0)
    sdk = device.get("sdk")
    if isinstance(sdk, str):
        values = [int(part) for part in re.findall(r"\d+", sdk)[:3]]
        return tuple((values + [0, 0, 0])[:3])  # type: ignore[return-value]
    return (0, 0, 0)


def _aliases(
    bindings: Mapping[str, object], platform: str, device_id: str
) -> list[str]:
    raw = bindings.get("aliases", {})
    configured = raw.get("flutter", {}) if isinstance(raw, dict) else {}
    if not isinstance(configured, dict):
        return []
    return [
        alias
        for alias, selected in configured.items()
        if isinstance(alias, str) and alias and selected == f"{platform}:{device_id}"
    ]


def _android_target(
    template: Mapping[str, object],
    device: Mapping[str, object],
    transport: str,
) -> tuple[dict[str, object], Mapping[str, object]]:
    configured = template.get("android")
    if not isinstance(configured, dict) or set(configured) != _ANDROID_FIELDS:
        raise SessionError(
            "unavailable", "Flutter Android platform probe is unavailable"
        )
    adb, identity = configured["adb"], device.get("android_identity")
    if (
        not isinstance(adb, str)
        or not os.path.isabs(adb)
        or not os.access(adb, os.X_OK)
        or not isinstance(identity, dict)
        or set(identity)
        != {
            "serial",
            "transport",
            "ro_serialno",
            "build_fingerprint",
            "product_device",
            "api_level",
            "avd_name",
            "usb_transport",
        }
        or identity["serial"] != device.get("id")
        or identity["transport"] != transport
    ):
        raise SessionError(
            "identity-unknown", "Flutter Android platform identity is unavailable"
        )
    platform_identity = {
        name: identity[name]
        for name in (
            "ro_serialno",
            "build_fingerprint",
            "product_device",
            "api_level",
            "avd_name",
            "usb_transport",
        )
    }
    return {
        "adb": adb,
        "serial": device["id"],
        "transport": transport,
        "identity": platform_identity,
    }, platform_identity


def _ios_inventory(xcrun: str, device_id: str) -> tuple[str, tuple[int, int, int]]:
    if not os.path.isabs(xcrun) or not os.access(xcrun, os.X_OK):
        raise SessionError("unavailable", "Flutter iOS platform tool is unavailable")
    try:
        value = json.loads(_run((xcrun, "simctl", "list", "--json")).decode("utf-8"))
        devices = value["devices"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SessionError("unknown", "Flutter iOS inventory is malformed") from error
    if not isinstance(devices, dict):
        raise SessionError("unknown", "Flutter iOS inventory is malformed")
    matches = [
        (runtime, item)
        for runtime, records in devices.items()
        if isinstance(runtime, str) and isinstance(records, list)
        for item in records
        if isinstance(item, dict)
        and item.get("udid") == device_id
        and item.get("isAvailable") is True
    ]
    if len(matches) != 1:
        raise SessionError("unavailable", "Flutter iOS simulator is unavailable")
    runtime, item = matches[0]
    data_path, device_type = item.get("dataPath"), item.get("deviceTypeIdentifier")
    if (
        not isinstance(data_path, str)
        or not os.path.isabs(data_path)
        or not isinstance(device_type, str)
        or not device_type
    ):
        raise SessionError(
            "identity-unknown", "Flutter iOS simulator identity is unavailable"
        )
    try:
        metadata = Path(data_path).stat()
    except OSError as error:
        raise SessionError(
            "identity-unknown", "Flutter iOS simulator identity is unavailable"
        ) from error
    rank = tuple(
        ([0, 0, 0] + [int(value) for value in re.findall(r"\d+", runtime)[-3:]])[-3:]
    )
    fingerprint = sha256(
        json.dumps(
            [
                device_id,
                runtime,
                device_type,
                data_path,
                metadata.st_dev,
                metadata.st_ino,
            ],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return fingerprint, rank


def _ios_target(
    template: Mapping[str, object],
    device: Mapping[str, object],
    transport: str,
) -> tuple[dict[str, object], str, tuple[int, int, int]]:
    if transport != "simulator":
        raise SessionError(
            "unavailable", "Flutter physical iOS requires a concrete native owner"
        )
    configured = template.get("ios")
    if not isinstance(configured, dict) or set(configured) != _IOS_FIELDS:
        raise SessionError("unavailable", "Flutter iOS simulator tool is unavailable")
    xcrun, device_id = configured["xcrun"], device["id"]
    if not isinstance(xcrun, str) or not isinstance(device_id, str):
        raise SessionError(
            "identity-unknown", "Flutter iOS simulator identity is unavailable"
        )
    fingerprint, rank = _ios_inventory(xcrun, device_id)
    return (
        {
            "probe_argv": [
                str(Path(sys.executable).resolve()),
                str(Path(__file__).resolve()),
                "--ios-probe",
                xcrun,
                device_id,
                transport,
            ],
            "foreground_argv": None,
            "capture_argv": None,
        },
        fingerprint,
        rank,
    )


def _android_inventory_devices(
    template: Mapping[str, object],
) -> list[dict[str, object]]:
    configured = template.get("android")
    if not isinstance(configured, dict) or set(configured) != _ANDROID_FIELDS:
        return []
    path = Path(__file__).resolve().parents[1] / "android-cli" / "backend.py"
    spec = importlib.util.spec_from_file_location(
        "mship_flutter_android_inventory", path
    )
    if spec is None or spec.loader is None:
        raise SessionError("unavailable", "Android read-only inventory is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result: list[dict[str, object]] = []
    for serial, (state, line) in module._devices().items():
        transport = module._transport(serial, line)
        if state != "device" or transport is None:
            continue
        identity = module._dynamic_identity(serial, line)
        result.append(
            {
                "id": serial,
                "targetPlatform": "android",
                "emulator": transport == "emulator",
                "isSupported": True,
                "capabilities": {"hotReload": True, "hotRestart": True},
                "android_identity": identity,
            }
        )
    return result


def _ios_inventory_devices(template: Mapping[str, object]) -> list[dict[str, object]]:
    configured = template.get("ios")
    if not isinstance(configured, dict) or set(configured) != _IOS_FIELDS:
        return []
    xcrun = configured["xcrun"]
    if not isinstance(xcrun, str):
        return []
    try:
        value = json.loads(_run((xcrun, "simctl", "list", "--json")).decode("utf-8"))
        devices = value["devices"]
    except KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, SessionError:
        return []
    if not isinstance(devices, dict):
        return []
    return [
        {
            "id": item["udid"],
            "targetPlatform": "ios",
            "emulator": True,
            "isSupported": True,
            "capabilities": {"hotReload": True, "hotRestart": True},
        }
        for records in devices.values()
        if isinstance(records, list)
        for item in records
        if isinstance(item, dict)
        and isinstance(item.get("udid"), str)
        and item.get("isAvailable") is True
    ]


def _candidate(
    template: Mapping[str, object],
    bindings: Mapping[str, object],
    device: Mapping[str, object],
    request: Mapping[str, object],
) -> dict[str, object]:
    platform, device_id = device.get("targetPlatform"), device.get("id")
    if (
        platform not in {"android", "ios"}
        or not isinstance(device_id, str)
        or not device_id
    ):
        raise SessionError("identity-unknown", "Flutter device identity is unavailable")
    transport = _transport(device)
    if transport is None or device.get("isSupported") is not True:
        raise SessionError("unavailable", "Flutter device is unavailable")
    capabilities = device.get("capabilities")
    if (
        not isinstance(capabilities, dict)
        or type(capabilities.get("hotReload")) is not bool
        or type(capabilities.get("hotRestart")) is not bool
    ):
        raise SessionError(
            "identity-unknown", "Flutter device capabilities are unavailable"
        )
    if platform == "android":
        platform_binding, identity = _android_target(template, device, transport)
        target_fingerprint = identity["build_fingerprint"]
        rank = _runtime_rank(device, platform, identity)
    else:
        platform_binding, target_fingerprint, rank = _ios_target(
            template, device, transport
        )
    if not isinstance(target_fingerprint, str):
        raise SessionError("identity-unknown", "Flutter device identity is unavailable")
    key = _device_key(platform, device_id)
    descriptor: dict[str, object] = {
        "target_key": key,
        "label": f"Flutter {platform} {transport} target",
        "tags": ["flutter", platform, transport],
        "roles": ["mobile"],
        "aliases": _aliases(bindings, platform, device_id),
        "preparation": [],
        "rank": [1, *rank],
        "platform": platform,
        "transport": transport,
        "target_fingerprint": target_fingerprint,
        "device_id": device_id,
        "app_id": template["app_id"],
        "modes": template["modes"],
        "flavors": template["flavors"],
        "capabilities": {
            "hot_reload": capabilities["hotReload"],
            "hot_restart": capabilities["hotRestart"],
        },
        platform: platform_binding,
    }
    target = {**descriptor, "binding": descriptor}
    parsed = FlutterTargetBinding.from_dict(target)
    options = FlutterOptions.from_dict(request["options"])
    if not parsed.supports(options):
        raise SessionError("unavailable", "Flutter mode or flavor is unavailable")
    advertised = ["run", "logs"]
    if platform == "android":
        advertised.append("capture")
    if options.mode == "debug" and descriptor["capabilities"]["hot_reload"]:
        advertised.append("reload")
    if options.mode == "debug" and descriptor["capabilities"]["hot_restart"]:
        advertised.append("restart")
    return {
        "target_key": key,
        "label": descriptor["label"],
        "tags": descriptor["tags"],
        "roles": descriptor["roles"],
        "aliases": descriptor["aliases"],
        "capabilities": advertised,
        "ready": True,
        "reason": None,
        "remediation": None,
        "preparation": [],
        "rank": descriptor["rank"],
        "binding": descriptor,
    }


def discover() -> int:
    request = load_request()
    bindings = load_bindings()
    try:
        template = _template(bindings)
    except ExampleError:
        emit_inventory(
            request,
            (),
            errors=(
                {
                    "code": "flutter_unavailable",
                    "message": "Flutter host bindings are unavailable",
                    "remediation": "Configure a host-private Flutter platform binding",
                },
            ),
        )
        return 0
    android_error = False
    try:
        android_devices = _android_inventory_devices(template)
    except SessionError:
        android_devices, android_error = [], True
    devices = android_devices + _ios_inventory_devices(template)
    candidates: list[dict[str, object]] = []
    for device in devices:
        try:
            candidates.append(_candidate(template, bindings, device, request))
        except SessionError as error:
            platform, device_id = device.get("targetPlatform"), device.get("id")
            if platform not in {"android", "ios"} or not isinstance(device_id, str):
                continue
            transport = _transport(device)
            key = _device_key(platform, device_id)
            candidates.append(
                {
                    "target_key": key,
                    "label": f"Flutter {platform} target",
                    "tags": ["flutter", platform]
                    + ([] if transport is None else [transport]),
                    "roles": ["mobile"],
                    "aliases": _aliases(bindings, platform, device_id),
                    "capabilities": [],
                    "ready": False,
                    "reason": error.code,
                    "remediation": "Check the exact configured Flutter platform target; discovery does not boot or provision it",
                    "preparation": [],
                    "rank": [0, *_runtime_rank(device, platform, None)],
                    "binding": {
                        "target_key": key,
                        "platform": platform,
                        "transport": transport,
                    },
                }
            )
    errors = ()
    if android_error:
        errors = (
            {
                "code": "android_inventory_unavailable",
                "message": "Android read-only inventory is unavailable",
                "remediation": "Start the existing Android platform service; discovery does not start ADB",
            },
        )
    emit_inventory(
        request,
        candidates,
        ("availability", "runtime_major", "runtime_minor", "runtime_patch"),
        errors,
    )
    return 0


def _owner_bindings() -> dict[str, object]:
    request = load_request()
    context = load_context()
    descriptor = context["private_binding"]
    if not isinstance(descriptor, dict) or descriptor.get("platform") not in {
        "android",
        "ios",
    }:
        raise ExampleError("selected Flutter binding requires an explicit platform")
    target = {**descriptor, "binding": descriptor}
    parsed = FlutterTargetBinding.from_dict(target)
    options = FlutterOptions.from_dict(request["options"])
    if not parsed.supports(options):
        raise ExampleError("selected Flutter mode or flavor is unavailable")
    template = _template(load_bindings())
    return {
        "paths": {
            "flutter": {
                "executable": template["executable"],
                "rank_schema": [
                    "availability",
                    "runtime_major",
                    "runtime_minor",
                    "runtime_patch",
                ],
                "targets": [target],
            }
        },
        "aliases": {},
    }


def _invoke_owner() -> int:
    document = _owner_bindings()
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="mship-flutter-",
        suffix=".json",
        delete=False,
    ) as stream:
        json.dump(document, stream, separators=(",", ":"))
        stream.flush()
        os.fchmod(stream.fileno(), 0o600)
        path = stream.name
    previous = os.environ.get("MSHIP_TARGET_BINDINGS_FILE")
    try:
        os.environ["MSHIP_TARGET_BINDINGS_FILE"] = path
        return owner_main()
    finally:
        if previous is None:
            os.environ.pop("MSHIP_TARGET_BINDINGS_FILE", None)
        else:
            os.environ["MSHIP_TARGET_BINDINGS_FILE"] = previous
        Path(path).unlink(missing_ok=True)


def _ios_probe(arguments: list[str]) -> int:
    if len(arguments) != 3:
        raise ExampleError("invalid Flutter iOS probe invocation")
    xcrun, device_id, transport = arguments
    if transport != "simulator":
        raise ExampleError("Flutter physical iOS probe is unavailable")
    fingerprint, _rank = _ios_inventory(xcrun, device_id)
    print(
        json.dumps(
            {
                "version": 1,
                "platform": "ios",
                "transport": transport,
                "target_fingerprint": fingerprint,
                "device_id": device_id,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0


def main() -> int:
    if sys.argv[1:] and sys.argv[1] == "--ios-probe":
        return _ios_probe(sys.argv[2:])
    if sys.argv[1:] == ["discover"]:
        return discover()
    if sys.argv[1:]:
        raise ExampleError("Flutter backend accepts no operation arguments")
    return _invoke_owner()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ExampleError, SessionError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
