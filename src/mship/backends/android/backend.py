"""Read-only Android inventory plus the reviewed Android session owner wrapper."""

from __future__ import annotations

from hashlib import sha256
import json
import os
import re
import socket
import sys
import tempfile
from pathlib import Path
from typing import Mapping

from mship.backends.common import (
    ExampleError,
    emit_inventory,
    load_bindings,
    load_context,
    load_request,
)
from mship.core.android_session import (
    AndroidBinding,
    AndroidProfileOptions,
    _component_identity,
    _resolved_component_identity,
    main as owner_main,
)
from mship.core.session_inputs import SessionError

_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_TEMPLATE_FIELDS = {
    "adb",
    "package",
    "component",
    "package_inspector",
    "capabilities",
    "fixtures",
}
_MAX_ADB_REPLY = 64 * 1024


def _server_port() -> int:
    value = os.environ.get("ANDROID_ADB_SERVER_PORT", "5037")
    try:
        port = int(value)
    except ValueError as error:
        raise SessionError(
            "unavailable", "Android inventory server is unavailable"
        ) from error
    if not 1 <= port <= 65535:
        raise SessionError("unavailable", "Android inventory server is unavailable")
    return port


def _send(sock: socket.socket, command: str) -> None:
    data = command.encode("utf-8")
    if len(data) > 0xFFFF:
        raise SessionError("invalid", "Android inventory request is invalid")
    sock.sendall(f"{len(data):04x}".encode("ascii") + data)


def _exact(sock: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise SessionError("unavailable", "Android inventory server is unavailable")
        result.extend(part)
    return bytes(result)


def _status(sock: socket.socket) -> None:
    status = _exact(sock, 4)
    if status == b"OKAY":
        return
    if status == b"FAIL":
        try:
            size = int(_exact(sock, 4), 16)
            _exact(sock, min(size, _MAX_ADB_REPLY))
        except ValueError, SessionError:
            pass
    raise SessionError("unavailable", "Android inventory request was rejected")


def _length_reply(sock: socket.socket) -> bytes:
    _status(sock)
    try:
        size = int(_exact(sock, 4), 16)
    except ValueError as error:
        raise SessionError(
            "unknown", "Android inventory response is malformed"
        ) from error
    if size > _MAX_ADB_REPLY:
        raise SessionError("unavailable", "Android inventory response is too large")
    return _exact(sock, size)


def _adb_service(command: str) -> bytes:
    try:
        with socket.create_connection(("127.0.0.1", _server_port()), timeout=2) as sock:
            sock.settimeout(5)
            _send(sock, command)
            return _length_reply(sock)
    except (OSError, UnicodeError) as error:
        raise SessionError(
            "unavailable", "Android inventory server is unavailable"
        ) from error


def _adb_shell(serial: str, command: str, *, getprop: bool = True) -> str:
    if not _SERIAL.fullmatch(serial):
        raise SessionError("invalid", "Android target identity is invalid")
    if not command or "\x00" in command or len(command) > 512:
        raise SessionError("invalid", "Android inventory request is invalid")
    try:
        with socket.create_connection(("127.0.0.1", _server_port()), timeout=2) as sock:
            sock.settimeout(5)
            _send(sock, f"host:transport:{serial}")
            _status(sock)
            _send(sock, f"shell:{'getprop ' if getprop else ''}{command}")
            _status(sock)
            result = bytearray()
            while True:
                part = sock.recv(8192)
                if not part:
                    break
                result.extend(part)
                if len(result) > _MAX_ADB_REPLY:
                    raise SessionError(
                        "unavailable", "Android inventory response is too large"
                    )
    except OSError as error:
        raise SessionError(
            "unavailable", "Android inventory server is unavailable"
        ) from error
    try:
        return bytes(result).decode("utf-8", "strict").strip()
    except UnicodeDecodeError as error:
        raise SessionError(
            "unknown", "Android inventory response is malformed"
        ) from error


def _devices() -> dict[str, tuple[str, str]]:
    try:
        lines = _adb_service("host:devices-l").decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError as error:
        raise SessionError(
            "unknown", "Android inventory response is malformed"
        ) from error
    devices: dict[str, tuple[str, str]] = {}
    for line in lines:
        fields = line.split()
        if len(fields) < 2 or not _SERIAL.fullmatch(fields[0]) or fields[0] in devices:
            raise SessionError("unknown", "Android inventory response is malformed")
        devices[fields[0]] = (fields[1], line)
    return devices


def _target_key(serial: str) -> str:
    return "android-" + sha256(serial.encode("utf-8")).hexdigest()[:40]


def _transport(serial: str, line: str) -> str | None:
    if "usb:" in line:
        return "usb"
    if serial.startswith("emulator-") or "product:" in line:
        return "emulator"
    return None


def _aliases(bindings: Mapping[str, object], serial: str) -> list[str]:
    raw = bindings.get("aliases", {})
    configured = raw.get("android", {}) if isinstance(raw, dict) else {}
    if not isinstance(configured, dict):
        return []
    return [
        alias
        for alias, target in configured.items()
        if isinstance(alias, str) and alias and target == serial
    ]


def _template(bindings: Mapping[str, object]) -> dict[str, object]:
    paths = bindings.get("paths")
    value = paths.get("android") if isinstance(paths, dict) else None
    if not isinstance(value, dict) or set(value) != _TEMPLATE_FIELDS:
        raise ExampleError("Android host bindings are unavailable")
    for name in ("adb", "package_inspector"):
        executable = value.get(name)
        if (
            not isinstance(executable, str)
            or not os.path.isabs(executable)
            or not os.access(executable, os.X_OK)
        ):
            raise ExampleError("configured Android tool is unavailable")
    return value


def _dynamic_identity(serial: str, line: str) -> dict[str, object]:
    transport = _transport(serial, line)
    if transport is None:
        raise SessionError("identity-unknown", "Android transport cannot be classified")
    properties = {
        "ro_serialno": _adb_shell(serial, "ro.serialno"),
        "build_fingerprint": _adb_shell(serial, "ro.build.fingerprint"),
        "product_device": _adb_shell(serial, "ro.product.device"),
        "api_level": _adb_shell(serial, "ro.build.version.sdk"),
        "avd_name": (
            _adb_shell(serial, "ro.boot.qemu.avd_name")
            or _adb_shell(serial, "ro.kernel.qemu.avd_name")
        )
        if transport == "emulator"
        else "",
    }
    if not all(
        properties[name]
        for name in ("ro_serialno", "build_fingerprint", "product_device", "api_level")
    ):
        raise SessionError("identity-unknown", "Android target identity is unavailable")
    try:
        api_level = int(properties["api_level"])
    except ValueError as error:
        raise SessionError(
            "identity-unknown", "Android API identity is unavailable"
        ) from error
    if api_level <= 0:
        raise SessionError("identity-unknown", "Android API identity is unavailable")
    usb = re.search(r"(?:^|\s)usb:([^\s]+)", line)
    if transport == "emulator" and not properties["avd_name"]:
        raise SessionError(
            "identity-unknown", "Android emulator identity is unavailable"
        )
    if transport == "usb" and usb is None:
        raise SessionError("identity-unknown", "Android USB identity is unavailable")
    return {
        "serial": serial,
        "transport": transport,
        "ro_serialno": properties["ro_serialno"],
        "build_fingerprint": properties["build_fingerprint"],
        "product_device": properties["product_device"],
        "api_level": api_level,
        "avd_name": properties["avd_name"] if transport == "emulator" else None,
        "usb_transport": usb.group(1) if transport == "usb" and usb else None,
    }


def _app_ready(binding: AndroidBinding) -> bool:
    package = _adb_shell(
        binding.serial, f"cmd package path {binding.package}", getprop=False
    )
    if not package.startswith("package:"):
        return False
    resolved = _adb_shell(
        binding.serial,
        f"cmd package resolve-activity --brief {binding.component}",
        getprop=False,
    )
    return _resolved_component_identity(resolved) == _component_identity(
        binding.package, binding.component
    )


def _dynamic_binding(
    template: Mapping[str, object], serial: str, line: str
) -> dict[str, object]:
    return {**template, **_dynamic_identity(serial, line)}


def _unready_candidate(
    serial: str, line: str, state: str, reason: str
) -> dict[str, object]:
    transport = _transport(serial, line)
    tags = ["android"] if transport is None else ["android", transport]
    return {
        "target_key": _target_key(serial),
        "label": "Android target"
        if transport is None
        else f"Android {transport} target",
        "tags": tags,
        "roles": ["mobile"],
        "aliases": _aliases(_BINDINGS, serial),
        "capabilities": [],
        "ready": False,
        "reason": reason,
        "remediation": "Authorize or make the exact Android target available; discovery does not start ADB",
        "preparation": [],
        "rank": [0, 0],
        "binding": {"target_key": _target_key(serial), "android": {"state": state}},
    }


_BINDINGS: Mapping[str, object] = {}


def discover() -> int:
    global _BINDINGS
    request = load_request()
    _BINDINGS = load_bindings()
    try:
        template = _template(_BINDINGS)
    except ExampleError as error:
        message = str(error)
        remediation = (
            "Install the configured Android tool without starting a new ADB server"
            if "tool" in message
            else "Configure a host-private Android app and tool template"
        )
        emit_inventory(
            request,
            (),
            errors=(
                {
                    "code": "android_unavailable",
                    "message": message,
                    "remediation": remediation,
                },
            ),
        )
        return 0
    try:
        devices = _devices()
    except SessionError:
        emit_inventory(
            request,
            (),
            ("availability", "android_api_level"),
            (
                {
                    "code": "android_unavailable",
                    "message": "Android read-only inventory is unavailable",
                    "remediation": "Start the existing ADB server and authorize a target; discovery does not start one",
                },
            ),
        )
        return 0
    candidates: list[dict[str, object]] = []
    for serial, (state, line) in devices.items():
        if state != "device":
            candidates.append(
                _unready_candidate(
                    serial,
                    line,
                    state,
                    {
                        "unauthorized": "unauthorized",
                        "offline": "busy",
                    }.get(state, "unavailable"),
                )
            )
            continue
        try:
            raw = _dynamic_binding(template, serial, line)
            binding = AndroidBinding.from_value(raw)
            AndroidProfileOptions.from_value(request["options"], binding)
            if not _app_ready(binding):
                raise SessionError(
                    "unavailable", "Configured Android app is unavailable"
                )
        except SessionError as error:
            candidates.append(_unready_candidate(serial, line, state, error.code))
            continue
        key = _target_key(serial)
        candidates.append(
            {
                "target_key": key,
                "label": f"Android {binding.transport} target",
                "tags": ["android", binding.transport],
                "roles": ["mobile"],
                "aliases": _aliases(_BINDINGS, serial),
                "capabilities": sorted(binding.capabilities),
                "ready": True,
                "reason": None,
                "remediation": None,
                "preparation": [],
                "rank": [1, binding.api_level],
                "binding": {"target_key": key, "android": raw},
            }
        )
    emit_inventory(request, candidates, ("availability", "android_api_level"))
    return 0


def _owner_bindings() -> dict[str, object]:
    request = load_request()
    context = load_context()
    selected = context["private_binding"]
    if not isinstance(selected, dict) or set(selected) != {"target_key", "android"}:
        raise ExampleError("selected Android binding is invalid")
    key, raw = selected["target_key"], selected["android"]
    if not isinstance(key, str):
        raise ExampleError("selected Android binding is invalid")
    binding = AndroidBinding.from_value(raw)
    AndroidProfileOptions.from_value(request["options"], binding)
    return {"paths": {"android": {"bindings": {key: raw}}}, "aliases": {}}


def _invoke_owner() -> int:
    bindings = _owner_bindings()
    with tempfile.NamedTemporaryFile(
        dir=Path(tempfile.gettempdir()).resolve(strict=True),
        mode="w",
        encoding="utf-8",
        prefix="mship-android-",
        suffix=".json",
        delete=False,
    ) as stream:
        json.dump(bindings, stream, separators=(",", ":"))
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


def main() -> int:
    if sys.argv[1:] == ["discover"]:
        return discover()
    if sys.argv[1:]:
        raise ExampleError("Android backend accepts no operation arguments")
    return _invoke_owner()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ExampleError, SessionError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
