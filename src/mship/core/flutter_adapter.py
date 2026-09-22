"""Private validation and platform primitives for Flutter machine sessions.

This module consumes only server-materialized target files.  It never discovers
or exposes a replacement target.
"""

from __future__ import annotations

import json
import os
import re
import select
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from mship.core.session_inputs import SessionError, strict_object

_TARGET_BINDINGS_FILE = "MSHIP_TARGET_BINDINGS_FILE"
_TARGET_REQUEST_FILE = "MSHIP_TARGET_REQUEST_FILE"
_TARGET_CONTEXT_FILE = "MSHIP_TARGET_CONTEXT_FILE"
_MAX_PRIVATE_FILE = 1024 * 1024
_SAFE_APP_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,255}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SAFE_FINGERPRINT = re.compile(r"^[A-Za-z0-9_.:/=-]{1,512}$")
_URL_RE = re.compile(
    r"(?i)(?:wss?|https?)://[^\s'\"]+|(?:token|auth|secret|key|credential)=[^\s&]+"
)


def _error(code: str = "invalid") -> SessionError:
    messages = {
        "invalid": "Invalid Flutter session configuration",
        "unavailable": "Flutter target capability is unavailable",
        "unknown": "Flutter target identity cannot be verified",
        "cancelled": "Flutter platform operation was cancelled",
    }
    return SessionError(code, messages[code])


def _private_object_from_environ(name: str) -> dict[str, object]:
    raw_path = os.environ.get(name)
    if raw_path is None:
        raise _error("unavailable")
    try:
        path = Path(raw_path)
        if not path.is_absolute() or ".." in path.parts:
            raise _error()
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        raise _error("unknown") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > _MAX_PRIVATE_FILE
        ):
            raise _error()
        body = bytearray()
        while len(body) <= _MAX_PRIVATE_FILE:
            part = os.read(descriptor, min(65536, _MAX_PRIVATE_FILE + 1 - len(body)))
            if not part:
                break
            body.extend(part)
        if len(body) > _MAX_PRIVATE_FILE:
            raise _error()
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, SessionError):
            raise
        raise _error() from exc
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise _error()
    return value


def _no_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate member")
        value[key] = item
    return value


def _required_string(value: object, *, regex: re.Pattern[str] = _SAFE_TOKEN) -> str:
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise _error()
    return value


def _exact_mapping(value: object, fields: set[str]) -> Mapping[str, object]:
    try:
        return strict_object(value, fields)
    except SessionError as exc:
        raise _error() from exc


@dataclass(frozen=True)
class FlutterOptions:
    """The only reviewed Flutter launch choices accepted by this owner."""

    entrypoint: str
    flavor: str | None
    mode: str
    platform: str | None = None
    transport: str | None = None

    @classmethod
    def from_dict(cls, value: object) -> "FlutterOptions":
        if not isinstance(value, dict) or set(value) not in (
            {"entrypoint", "flavor", "mode"},
            {"entrypoint", "flavor", "mode", "platform", "transport"},
        ):
            raise _error()
        entrypoint, flavor, mode = value["entrypoint"], value["flavor"], value["mode"]
        platform, transport = value.get("platform"), value.get("transport")
        if (
            not isinstance(entrypoint, str)
            or not entrypoint.startswith("lib/")
            or len(entrypoint) > 256
            or "\\" in entrypoint
            or any(part in {"", ".", ".."} for part in entrypoint.split("/"))
            or not entrypoint.endswith(".dart")
        ):
            raise _error()
        if flavor is not None and (
            not isinstance(flavor, str) or not _SAFE_TOKEN.fullmatch(flavor)
        ):
            raise _error()
        if mode not in {"debug", "profile", "release"}:
            raise _error("unavailable")
        if platform is not None and platform not in {"android", "ios"}:
            raise _error("invalid")
        if transport is not None and transport not in {"usb", "emulator", "simulator"}:
            raise _error("invalid")
        if platform == "android" and transport == "simulator":
            raise _error("invalid")
        if platform == "ios" and transport == "emulator":
            raise _error("invalid")
        return cls(entrypoint, flavor, mode, platform, transport)


@dataclass(frozen=True)
class AndroidBinding:
    adb: str = field(repr=False)
    serial: str = field(repr=False)
    transport: str
    identity: dict[str, object] = field(repr=False)

    @classmethod
    def from_dict(cls, value: object) -> "AndroidBinding":
        data = _exact_mapping(value, {"adb", "serial", "transport", "identity"})
        adb = data["adb"]
        serial = data["serial"]
        transport = data["transport"]
        if (
            not isinstance(adb, str)
            or not Path(adb).is_absolute()
            or ".." in Path(adb).parts
            or not isinstance(serial, str)
            or not serial
            or "\x00" in serial
            or transport not in {"usb", "emulator"}
        ):
            raise _error()
        identity_data = _exact_mapping(
            data["identity"],
            {
                "ro_serialno",
                "build_fingerprint",
                "product_device",
                "api_level",
                "avd_name",
                "usb_transport",
            },
        )
        if (
            not isinstance(identity_data["ro_serialno"], str)
            or not isinstance(identity_data["build_fingerprint"], str)
            or not isinstance(identity_data["product_device"], str)
            or type(identity_data["api_level"]) is not int
            or (
                identity_data["avd_name"] is not None
                and not isinstance(identity_data["avd_name"], str)
            )
            or (
                identity_data["usb_transport"] is not None
                and not isinstance(identity_data["usb_transport"], str)
            )
        ):
            raise _error()
        return cls(adb, serial, transport, dict(identity_data))


@dataclass(frozen=True)
class IosBinding:
    probe_argv: tuple[str, ...] = field(repr=False)
    foreground_argv: tuple[str, ...] | None = field(repr=False)
    capture_argv: tuple[str, ...] | None = field(repr=False)

    @property
    def supports_capture(self) -> bool:
        return self.foreground_argv is not None and self.capture_argv is not None

    @classmethod
    def from_dict(cls, value: object) -> "IosBinding":
        data = _exact_mapping(value, {"probe_argv", "foreground_argv", "capture_argv"})
        return cls(
            _configured_argv(data["probe_argv"]),
            _optional_configured_argv(data["foreground_argv"]),
            _optional_configured_argv(data["capture_argv"]),
        )


def _configured_argv(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 32
        or not all(
            isinstance(item, str) and item and "\x00" not in item and len(item) <= 1024
            for item in value
        )
    ):
        raise _error()
    executable = Path(value[0])
    if not executable.is_absolute() or ".." in executable.parts:
        raise _error()
    return tuple(value)


def _optional_configured_argv(value: object) -> tuple[str, ...] | None:
    return None if value is None else _configured_argv(value)


@dataclass(frozen=True)
class FlutterTargetBinding:
    """One exact #530 candidate, including its platform-only revalidation data.

    ``{"paths":{"flutter":{"executable": "/.../flutter", "rank_schema":[...],
    "targets":[...] }},"aliases":{}}``.  Every target has strict discovery metadata
    (``target_key``, ``label``, ``tags``, ``roles``, ``aliases``,
    ``preparation``, and ``rank``), exact candidate ``binding``, platform receipt,
    reviewed app/mode/flavor capability, and exactly one platform block.  The
    ``binding`` object must equal the sealed candidate ``private_binding`` exactly.
    """

    target_key: str
    label: str
    tags: tuple[str, ...]
    roles: tuple[str, ...]
    aliases: tuple[str, ...]
    preparation: tuple[str, ...]
    rank: tuple[int, ...]
    binding: dict[str, object] = field(repr=False)
    platform: str
    transport: str
    target_fingerprint: str = field(repr=False)
    device_id: str = field(repr=False)
    app_id: str = field(repr=False)
    modes: tuple[str, ...]
    flavors: tuple[str | None, ...]
    hot_reload: bool
    hot_restart: bool
    android: AndroidBinding | None = field(repr=False)
    ios: IosBinding | None = field(repr=False)

    @classmethod
    def from_dict(cls, value: object) -> "FlutterTargetBinding":
        if not isinstance(value, dict) or value.get("platform") not in {
            "android",
            "ios",
        }:
            raise _error()
        platform = value["platform"]
        assert isinstance(platform, str)
        platform_field = "android" if platform == "android" else "ios"
        data = _exact_mapping(
            value,
            {
                "target_key",
                "label",
                "tags",
                "roles",
                "aliases",
                "preparation",
                "rank",
                "binding",
                "platform",
                "transport",
                "target_fingerprint",
                "device_id",
                "app_id",
                "modes",
                "flavors",
                "capabilities",
                platform_field,
            },
        )
        platform = data["platform"]
        transport = data["transport"]
        metadata = (
            data["target_key"],
            data["label"],
            data["tags"],
            data["roles"],
            data["aliases"],
            data["preparation"],
            data["rank"],
        )

        def tokens(value: object) -> bool:
            return (
                isinstance(value, list)
                and all(
                    isinstance(item, str) and _SAFE_TOKEN.fullmatch(item)
                    for item in value
                )
                and len(set(value)) == len(value)
            )

        if (
            not isinstance(platform, str)
            or platform not in {"android", "ios"}
            or not isinstance(transport, str)
            or not _SAFE_TOKEN.fullmatch(transport)
            or not isinstance(data["binding"], dict)
            or not isinstance(metadata[1], str)
            or not metadata[1]
            or len(metadata[1]) > 1024
            or not isinstance(metadata[0], str)
            or not _SAFE_TOKEN.fullmatch(metadata[0])
            or any(not tokens(values) for values in metadata[2:6])
            or not isinstance(metadata[6], list)
            or not metadata[6]
            or any(type(item) is not int for item in metadata[6])
        ):
            raise _error()
        fingerprint = _required_string(
            data["target_fingerprint"], regex=_SAFE_FINGERPRINT
        )
        device_id = _required_string(data["device_id"], regex=_SAFE_TOKEN)
        app_id = _required_string(data["app_id"], regex=_SAFE_APP_ID)
        modes = data["modes"]
        flavors = data["flavors"]
        capabilities = _exact_mapping(
            data["capabilities"], {"hot_reload", "hot_restart"}
        )
        if (
            not isinstance(modes, list)
            or not modes
            or any(item not in {"debug", "profile", "release"} for item in modes)
            or len(set(modes)) != len(modes)
            or not isinstance(flavors, list)
            or not flavors
            or any(
                item is not None
                and (not isinstance(item, str) or not _SAFE_TOKEN.fullmatch(item))
                for item in flavors
            )
            or len(set(flavors)) != len(flavors)
            or type(capabilities["hot_reload"]) is not bool
            or type(capabilities["hot_restart"]) is not bool
        ):
            raise _error()
        prefix = (
            metadata[0],
            metadata[1],
            tuple(metadata[2]),
            tuple(metadata[3]),
            tuple(metadata[4]),
            tuple(metadata[5]),
            tuple(metadata[6]),
            dict(data["binding"]),
            platform,
            transport,
            fingerprint,
            device_id,
            app_id,
            tuple(modes),
            tuple(flavors),
            capabilities["hot_reload"],
            capabilities["hot_restart"],
        )
        if platform == "android":
            android = AndroidBinding.from_dict(data["android"])
            if transport != android.transport or device_id != android.serial:
                raise _error()
            return cls(*prefix, android, None)
        ios = IosBinding.from_dict(data["ios"])
        return cls(*prefix, None, ios)

    def supports(self, options: FlutterOptions) -> bool:
        return (
            options.mode in self.modes
            and options.flavor in self.flavors
            and (options.platform is None or options.platform == self.platform)
            and (options.transport is None or options.transport == self.transport)
        )


@dataclass(frozen=True)
class FlutterLaunchConfig:
    executable: str = field(repr=False)
    target: FlutterTargetBinding
    options: FlutterOptions
    context: dict[str, object] = field(repr=False)

    def argv(self) -> tuple[str, ...]:
        mode_args = {"debug": (), "profile": ("--profile",), "release": ("--release",)}[
            self.options.mode
        ]
        flavor_args = (
            () if self.options.flavor is None else ("--flavor", self.options.flavor)
        )
        return (
            self.executable,
            "run",
            "--machine",
            "--device-id",
            self.target.device_id,
            "--target",
            self.options.entrypoint,
            *flavor_args,
            *mode_args,
        )


def load_launch_config() -> FlutterLaunchConfig:
    """Load one sealed envelope, validated #530 request and local binding entry."""
    request = _private_object_from_environ(_TARGET_REQUEST_FILE)
    context = _private_object_from_environ(_TARGET_CONTEXT_FILE)
    bindings = _private_object_from_environ(_TARGET_BINDINGS_FILE)
    expected_context = {
        "protocol_version",
        "run_id",
        "task",
        "repo",
        "profile",
        "profile_revision",
        "backend",
        "backend_revision",
        "host_name",
        "host_scope",
        "host_endpoint_fingerprint",
        "operation",
        "capabilities",
        "private_binding",
        "session_owner",
        "task_keys",
    }
    if (
        set(context) != expected_context
        or context.get("protocol_version") != 1
        or context.get("session_owner") != "flutter"
    ):
        raise _error()
    if not isinstance(context["private_binding"], dict) or not isinstance(
        context["task_keys"], dict
    ):
        raise _error()
    required_request = {
        "protocol_version",
        "backend",
        "backend_revision",
        "profile",
        "profile_revision",
        "task",
        "repo",
        "operation",
        "options",
        "target_alias",
    }
    if set(request) != required_request or request.get("protocol_version") != 1:
        raise _error()
    for name in (
        "backend",
        "backend_revision",
        "profile",
        "profile_revision",
        "task",
        "repo",
        "operation",
    ):
        if request.get(name) != context.get(name):
            raise _error()
    options = FlutterOptions.from_dict(request["options"])
    root = _exact_mapping(bindings, {"paths", "aliases"})
    paths = root["paths"]
    if not isinstance(paths, dict):
        raise _error()
    flutter = _exact_mapping(
        paths.get("flutter"), {"executable", "rank_schema", "targets"}
    )
    executable = flutter["executable"]
    targets = flutter["targets"]
    rank_schema = flutter["rank_schema"]
    if (
        not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or ".." in Path(executable).parts
        or not isinstance(rank_schema, list)
        or not rank_schema
        or not all(
            isinstance(item, str) and _SAFE_TOKEN.fullmatch(item)
            for item in rank_schema
        )
        or len(set(rank_schema)) != len(rank_schema)
        or not isinstance(targets, list)
        or not 1 <= len(targets) <= 32
    ):
        raise _error()
    candidates = [FlutterTargetBinding.from_dict(item) for item in targets]
    if any(len(candidate.rank) != len(rank_schema) for candidate in candidates):
        raise _error()
    matches = [
        candidate
        for candidate in candidates
        if candidate.binding == context["private_binding"]
    ]
    if len(matches) != 1 or not matches[0].supports(options):
        raise _error("unavailable")
    return FlutterLaunchConfig(executable, matches[0], options, context)


def load_discovery_config() -> tuple[
    dict[str, object],
    FlutterOptions,
    str,
    tuple[str, ...],
    tuple[FlutterTargetBinding, ...],
]:
    """Load the fixed configured discover-task inputs without an owner context."""
    request = _private_object_from_environ(_TARGET_REQUEST_FILE)
    bindings = _private_object_from_environ(_TARGET_BINDINGS_FILE)
    required_request = {
        "protocol_version",
        "backend",
        "backend_revision",
        "profile",
        "profile_revision",
        "task",
        "repo",
        "operation",
        "options",
        "target_alias",
    }
    if set(request) != required_request or request.get("protocol_version") != 1:
        raise _error()
    if not all(
        isinstance(request.get(field), str) and request[field]
        for field in (
            "backend",
            "backend_revision",
            "profile",
            "profile_revision",
            "task",
            "repo",
            "operation",
        )
    ):
        raise _error()
    options = FlutterOptions.from_dict(request["options"])
    root = _exact_mapping(bindings, {"paths", "aliases"})
    if not isinstance(root["paths"], dict):
        raise _error()
    flutter = _exact_mapping(
        root["paths"].get("flutter"), {"executable", "rank_schema", "targets"}
    )
    executable, rank_schema, targets = (
        flutter["executable"],
        flutter["rank_schema"],
        flutter["targets"],
    )
    if (
        not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or ".." in Path(executable).parts
        or not isinstance(rank_schema, list)
        or not rank_schema
        or not all(
            isinstance(item, str) and _SAFE_TOKEN.fullmatch(item)
            for item in rank_schema
        )
        or len(set(rank_schema)) != len(rank_schema)
        or not isinstance(targets, list)
        or not 1 <= len(targets) <= 32
    ):
        raise _error()
    parsed = tuple(FlutterTargetBinding.from_dict(item) for item in targets)
    if any(len(target.rank) != len(rank_schema) for target in parsed):
        raise _error()
    return request, options, executable, tuple(rank_schema), parsed


def discovery_candidate(
    target: FlutterTargetBinding, options: FlutterOptions, executable: str
) -> dict[str, object]:
    """Build one bounded #530 candidate only from its configured exact receipt."""
    ready = target.supports(options)
    if ready:
        try:
            revalidate_target(target)
        except SessionError:
            ready = False
    capabilities = ["run", "logs"]
    if target.platform == "android" or (
        target.ios is not None and target.ios.supports_capture
    ):
        capabilities.append("capture")
    if ready and options.mode == "debug" and target.hot_reload:
        capabilities.append("reload")
    if ready and options.mode == "debug" and target.hot_restart:
        capabilities.append("restart")
    return {
        "target_key": target.target_key,
        "label": target.label,
        "tags": list(target.tags),
        "roles": list(target.roles),
        "aliases": list(target.aliases),
        "capabilities": capabilities,
        "ready": ready,
        "reason": None if ready else "target_unavailable",
        "remediation": None if ready else "Selected Flutter target is unavailable",
        "preparation": list(target.preparation),
        "rank": list(target.rank),
        "binding": target.binding,
    }


def redact_log(value: str) -> str:
    """Return bounded user text without control URLs or credential-like fragments."""
    if not isinstance(value, str):
        return ""
    return _URL_RE.sub("[redacted]", value)[: 16 * 1024]


def _bounded_run(
    argv: tuple[str, ...],
    *,
    environment: Mapping[str, str],
    timeout: float,
    cancel: threading.Event | None = None,
    capture_stdout: bool = True,
) -> bytes:
    """Collect at most 64KiB while retaining ownership of the exact child."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(environment),
        )
    except OSError as exc:
        raise _error("unavailable") from exc
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                raise _error("cancelled")
            if time.monotonic() >= deadline:
                raise _error("unavailable")
            if proc.stdout is None:
                time.sleep(0.05)
                continue
            ready, _, _ = select.select([proc.stdout], [], [], 0.05)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 8192)
                output.extend(chunk)
                if len(output) > 64 * 1024:
                    raise _error("unavailable")
        if proc.stdout is not None:
            while True:
                chunk = os.read(proc.stdout.fileno(), 8192)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 64 * 1024:
                    raise _error("unavailable")
        if proc.returncode != 0:
            raise _error("unknown")
        return bytes(output)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)


def _run_receipt(
    argv: tuple[str, ...],
    *,
    expected: FlutterTargetBinding,
    foreground: bool,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> dict[str, object]:
    try:
        raw = _bounded_run(
            argv, environment=_tool_environment(), timeout=timeout, cancel=cancel
        )
    except SessionError:
        raise
    fields = {"version", "platform", "transport", "target_fingerprint", "device_id"}
    if foreground:
        fields.add("app_identity")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
        receipt = _exact_mapping(value, fields)
    except (UnicodeError, ValueError, json.JSONDecodeError, SessionError) as exc:
        raise _error("unknown") from exc
    if (
        receipt["version"] != 1
        or receipt["platform"] != expected.platform
        or receipt["transport"] != expected.transport
        or receipt["target_fingerprint"] != expected.target_fingerprint
        or receipt["device_id"] != expected.device_id
        or (foreground and receipt["app_identity"] != expected.app_id)
    ):
        raise _error("unknown")
    return dict(receipt)


def _tool_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("MSHIP_")
    }
    if extra:
        environment.update(extra)
    return environment


def reported_flutter_capabilities(
    executable: str, target: FlutterTargetBinding
) -> tuple[bool, bool]:
    """Read the exact selected device's nested Flutter capability report."""
    if not isinstance(executable, str) or not Path(executable).is_absolute():
        raise _error()
    raw = _bounded_run(
        (executable, "devices", "--machine"),
        environment=_tool_environment(),
        timeout=10,
    )
    try:
        devices = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise _error("unknown") from exc
    if not isinstance(devices, list):
        raise _error("unknown")
    exact = [
        item
        for item in devices
        if isinstance(item, dict) and item.get("id") == target.device_id
    ]
    if len(exact) != 1:
        raise _error("unavailable")
    device = exact[0]
    capabilities = device.get("capabilities")
    if (
        device.get("targetPlatform") != target.platform
        or not isinstance(capabilities, dict)
        or type(capabilities.get("hotReload")) is not bool
        or type(capabilities.get("hotRestart")) is not bool
    ):
        raise _error("unknown")
    return capabilities["hotReload"], capabilities["hotRestart"]


def revalidate_target(
    target: FlutterTargetBinding, cancel: threading.Event | None = None
) -> None:
    """Fail closed against the selected target; no discovery or replacement occurs."""
    if target.platform == "android":
        assert target.android is not None
        from mship.core.android_adapter import probe_android

        observed = probe_android(
            target.android.adb, target.android.serial, target.android.transport
        )
        if observed != {
            "serial": target.android.serial,
            "transport": target.android.transport,
            **target.android.identity,
        }:
            raise _error("unknown")
        if observed.get("build_fingerprint") != target.target_fingerprint:
            raise _error("unknown")
        return
    assert target.ios is not None
    _run_receipt(
        target.ios.probe_argv, expected=target, foreground=False, cancel=cancel
    )


def attest_ios_foreground(
    target: FlutterTargetBinding, cancel: threading.Event | None = None
) -> None:
    assert target.ios is not None
    if target.ios.foreground_argv is None:
        raise _error("unavailable")
    _run_receipt(
        target.ios.foreground_argv, expected=target, foreground=True, cancel=cancel
    )


def capture_ios(
    target: FlutterTargetBinding,
    directory: Path,
    kinds: tuple[str, ...],
    cancel: threading.Event,
) -> None:
    assert target.ios is not None
    if not target.ios.supports_capture:
        raise _error("unavailable")
    assert target.ios.foreground_argv is not None
    assert target.ios.capture_argv is not None
    _run_receipt(
        target.ios.foreground_argv, expected=target, foreground=True, cancel=cancel
    )
    _bounded_run(
        target.ios.capture_argv,
        environment=_tool_environment(
            {
                "MSHIP_CAPTURE_DIR": str(directory),
                "MSHIP_CAPTURE_KINDS": ",".join(kinds),
                "MSHIP_CAPTURE_PLATFORM": "ios",
            }
        ),
        timeout=30,
        cancel=cancel,
        capture_stdout=False,
    )
