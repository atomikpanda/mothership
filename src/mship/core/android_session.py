"""The private, long-lived native Android owner.

Only sealed target inputs and server-issued owner capabilities enter this module.
Its public results deliberately omit adb serials, tool paths, command output, PIDs,
and resource handles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from typing import BinaryIO, Callable, Mapping

from mship.core.android_adapter import (
    _adb,
    _run,
    capture_android,
    file_digest,
    foreground_android,
    probe_android,
)
from mship.core.run_target.models import DiscoveryRequest
from mship.core.session_channel import (
    OwnerClient,
    OwnerContext,
    _write_receipt,
    acquire_app_lease,
    read_private_json,
    serve_owner,
)
from mship.core.session_inputs import OwnerRequest, SessionError, strict_object

TARGET_CONTEXT_FILE = "MSHIP_TARGET_CONTEXT_FILE"
TARGET_REQUEST_FILE = "MSHIP_TARGET_REQUEST_FILE"
TARGET_BINDINGS_FILE = "MSHIP_TARGET_BINDINGS_FILE"
SESSION_OPERATION_FILE = "MSHIP_SESSION_OPERATION_FILE"
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_COMPONENT = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+/(?:[A-Za-z][A-Za-z0-9_.$]*|\.[A-Za-z][A-Za-z0-9_.$]*)$"
)
_FIXTURE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_TASK_KEY = re.compile(r"^[A-Za-z0-9:_./-]{1,128}$")
_TCP = re.compile(r"^tcp:([1-9][0-9]{0,4})$")


def _error(code: str, message: str) -> SessionError:
    return SessionError(code, message)


def _strict_json_file(name: str) -> dict[str, object]:
    value = os.environ.get(name)
    if value is None:
        raise _error("unavailable", "Required private session input is unavailable")
    try:
        return read_private_json(Path(value))
    except (OSError, SessionError) as error:
        raise _error(
            "unknown", "Required private session input is unavailable"
        ) from error


def _text(
    value: object,
    *,
    pattern: re.Pattern[str] | None = None,
    field: str = "configuration",
) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise _error("invalid", f"Invalid Android {field}")
    if pattern is not None and not pattern.fullmatch(value):
        raise _error("invalid", f"Invalid Android {field}")
    return value


def _absolute(value: object, *, field: str) -> str:
    text = _text(value, field=field)
    if not os.path.isabs(text):
        raise _error("invalid", f"Invalid Android {field}")
    return text


def _port(value: object) -> str:
    text = _text(value, field="fixture endpoint")
    match = _TCP.fullmatch(text)
    if match is None or int(match.group(1)) > 65535:
        raise _error("invalid", "Invalid Android fixture endpoint")
    return text


@dataclass(frozen=True)
class Fixture:
    kind: str
    local: str
    remote: str

    @classmethod
    def from_value(cls, value: object) -> "Fixture":
        data = strict_object(value, {"kind", "local", "remote"})
        kind = _text(data["kind"], field="fixture kind")
        if kind not in {"forward", "reverse"}:
            raise _error("invalid", "Invalid Android fixture kind")
        return cls(kind, _port(data["local"]), _port(data["remote"]))


def _fixture_mappings(
    kind: str, selected_device: str, output: bytes
) -> list[tuple[str, str, str]]:
    """Parse an adb mapping list as (device, local, remote) identities."""
    try:
        lines = output.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise _error(
            "unknown", "Android fixture mapping listing is malformed"
        ) from error
    mappings: list[tuple[str, str, str]] = []
    for line in lines:
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3:
            raise _error("unknown", "Android fixture mapping listing is malformed")
        if kind == "forward":
            device, local, remote = fields
        else:
            # reverse --list is scoped by -s. Its first column is only a transport
            # label, so the selected device is the authoritative device identity.
            _transport, remote, local = fields
            device = selected_device
        if not device or not local or not remote:
            raise _error("unknown", "Android fixture mapping listing is malformed")
        mappings.append((device, local, remote))
    return mappings


@dataclass(frozen=True)
class AndroidBinding:
    adb: str
    serial: str = field(repr=False)
    transport: str
    ro_serialno: str = field(repr=False)
    build_fingerprint: str = field(repr=False)
    product_device: str = field(repr=False)
    api_level: int
    avd_name: str | None = field(repr=False)
    usb_transport: str | None = field(repr=False)
    package: str = field(repr=False)
    component: str = field(repr=False)
    package_inspector: str = field(repr=False)
    capabilities: frozenset[str]
    fixtures: Mapping[str, Fixture] = field(repr=False)

    @classmethod
    def from_value(cls, value: object) -> "AndroidBinding":
        data = strict_object(
            value,
            {
                "adb",
                "serial",
                "transport",
                "ro_serialno",
                "build_fingerprint",
                "product_device",
                "api_level",
                "avd_name",
                "usb_transport",
                "package",
                "component",
                "package_inspector",
                "capabilities",
                "fixtures",
            },
        )
        transport = _text(data["transport"], field="transport")
        if transport not in {"emulator", "usb"}:
            raise _error("invalid", "Invalid Android transport")
        api_level = data["api_level"]
        if type(api_level) is not int or not 1 <= api_level <= 1000:
            raise _error("invalid", "Invalid Android API identity")
        avd_name, usb_transport = data["avd_name"], data["usb_transport"]
        if transport == "emulator":
            if (
                not isinstance(avd_name, str)
                or not avd_name
                or usb_transport is not None
            ):
                raise _error("invalid", "Invalid Android emulator binding")
        elif (
            not isinstance(usb_transport, str)
            or not usb_transport
            or avd_name is not None
        ):
            raise _error("invalid", "Invalid Android USB binding")
        package = _text(data["package"], pattern=_PACKAGE, field="package")
        component = _text(data["component"], pattern=_COMPONENT, field="component")
        if not component.startswith(package + "/"):
            raise _error("invalid", "Android component does not match package")
        raw_capabilities = data["capabilities"]
        if (
            not isinstance(raw_capabilities, list)
            or not raw_capabilities
            or not all(
                isinstance(item, str) and _TASK_KEY.fullmatch(item)
                for item in raw_capabilities
            )
            or len(set(raw_capabilities)) != len(raw_capabilities)
        ):
            raise _error("invalid", "Invalid Android capability configuration")
        raw_fixtures = data["fixtures"]
        if not isinstance(raw_fixtures, dict) or len(raw_fixtures) > 64:
            raise _error("invalid", "Invalid Android fixture configuration")
        fixtures: dict[str, Fixture] = {}
        for fixture_id, fixture in raw_fixtures.items():
            fixtures[_text(fixture_id, pattern=_FIXTURE, field="fixture identity")] = (
                Fixture.from_value(fixture)
            )
        return cls(
            _absolute(data["adb"], field="adb tool"),
            _text(data["serial"], field="serial"),
            transport,
            _text(data["ro_serialno"], field="serial identity"),
            _text(data["build_fingerprint"], field="build identity"),
            _text(data["product_device"], field="product identity"),
            api_level,
            avd_name,
            usb_transport,
            package,
            component,
            _absolute(data["package_inspector"], field="package inspector"),
            frozenset(raw_capabilities),
            fixtures,
        )


@dataclass(frozen=True)
class AndroidProfileOptions:
    package: str
    component: str
    instrumentation: str | None
    platform: str | None = None
    transport: str | None = None

    @classmethod
    def from_value(
        cls, value: object, binding: AndroidBinding
    ) -> "AndroidProfileOptions":
        if not isinstance(value, dict) or set(value) not in (
            {"package", "component", "instrumentation"},
            {"package", "component", "instrumentation", "platform", "transport"},
        ):
            raise _error("invalid", "Invalid Android profile options")
        package = _text(value["package"], pattern=_PACKAGE, field="profile package")
        component = _text(
            value["component"], pattern=_COMPONENT, field="profile component"
        )
        instrument = value["instrumentation"]
        if instrument is not None:
            instrument = _text(instrument, pattern=_COMPONENT, field="instrumentation")
        platform, transport = value.get("platform"), value.get("transport")
        if platform is not None and platform != "android":
            raise _error("unavailable", "Android profile platform is unavailable")
        if transport is not None and transport not in {"usb", "emulator"}:
            raise _error("invalid", "Invalid Android profile transport")
        if transport is not None and transport != binding.transport:
            raise _error("unavailable", "Android profile transport is unavailable")
        if package != binding.package or component != binding.component:
            raise _error("invalid", "Android profile does not match selected target")
        return cls(package, component, instrument, platform, transport)


@dataclass(frozen=True)
class TargetEnvelope:
    run_id: str
    operation: str
    capabilities: frozenset[str]
    task_keys: Mapping[str, str]
    binding: AndroidBinding
    options: AndroidProfileOptions

    @classmethod
    def load(cls, context: OwnerContext) -> "TargetEnvelope":
        data = _strict_json_file(TARGET_CONTEXT_FILE)
        expected = {
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
        strict_object(data, expected)
        if (
            data["protocol_version"] != 1
            or data["task"] != context.task
            or data["repo"] != context.repo
        ):
            raise _error("invalid", "Sealed Android target does not match session")
        if data["session_owner"] != "android":
            raise _error("invalid", "Sealed target belongs to another session owner")
        run_id = _text(data["run_id"], field="run identity")
        operation = _text(data["operation"], field="operation")
        capabilities_value = data["capabilities"]
        if not isinstance(capabilities_value, list) or not all(
            isinstance(item, str) for item in capabilities_value
        ):
            raise _error("invalid", "Invalid Android capabilities")
        task_keys_raw = data["task_keys"]
        if not isinstance(task_keys_raw, dict) or not task_keys_raw:
            raise _error("invalid", "Invalid Android task configuration")
        task_keys = {
            _text(key, field="operation"): _text(
                value, pattern=_TASK_KEY, field="logical task"
            )
            for key, value in task_keys_raw.items()
        }
        if operation not in task_keys:
            raise _error("invalid", "Android primary operation is not configured")
        private_binding = strict_object(
            data["private_binding"], {"target_key", "android"}
        )
        target_key = _text(private_binding["target_key"], field="target identity")
        binding = AndroidBinding.from_value(private_binding["android"])
        request_raw = _strict_json_file(TARGET_REQUEST_FILE)
        try:
            request = DiscoveryRequest.model_validate(request_raw)
        except Exception as error:
            raise _error("invalid", "Invalid sealed Android profile request") from error
        if (
            request.task != context.task
            or request.repo != context.repo
            or request.operation != operation
        ):
            raise _error(
                "invalid", "Sealed Android profile request does not match session"
            )
        options = AndroidProfileOptions.from_value(request.options, binding)
        local = _strict_json_file(TARGET_BINDINGS_FILE)
        local_data = strict_object(local, {"paths", "aliases"})
        paths = local_data["paths"]
        if not isinstance(paths, dict):
            raise _error("invalid", "Invalid Android host binding configuration")
        configured = paths.get("android")
        if not isinstance(configured, dict) or set(configured) != {"bindings"}:
            raise _error("invalid", "Android host binding is unavailable")
        bindings = configured["bindings"]
        if (
            not isinstance(bindings, dict)
            or bindings.get(target_key) != private_binding["android"]
        ):
            raise _error(
                "identity-unknown",
                "Selected Android binding no longer matches this host",
            )
        if frozenset(capabilities_value) != binding.capabilities:
            raise _error(
                "identity-unknown",
                "Selected Android capabilities no longer match host binding",
            )
        return cls(run_id, operation, binding.capabilities, task_keys, binding, options)


def discover_main() -> int:
    """Emit the configured Android candidates without requiring an owner context."""
    request_raw = _strict_json_file(TARGET_REQUEST_FILE)
    try:
        request = DiscoveryRequest.model_validate(request_raw)
    except Exception as error:
        raise _error("invalid", "Invalid Android discovery request") from error
    local = _strict_json_file(TARGET_BINDINGS_FILE)
    paths = strict_object(local, {"paths", "aliases"})["paths"]
    if not isinstance(paths, dict):
        raise _error("invalid", "Invalid Android host binding configuration")
    configured = paths.get("android")
    if not isinstance(configured, dict) or set(configured) != {"bindings"}:
        raise _error("invalid", "Android host binding is unavailable")
    raw_bindings = configured["bindings"]
    if not isinstance(raw_bindings, dict) or not raw_bindings:
        raise _error("unavailable", "No Android target is configured on this host")
    candidates: list[dict[str, object]] = []
    for target_key, raw_binding in sorted(raw_bindings.items()):
        safe_key = _text(target_key, pattern=_FIXTURE, field="target identity")
        binding = AndroidBinding.from_value(raw_binding)
        try:
            AndroidProfileOptions.from_value(request.options, binding)
            observed = probe_android(binding.adb, binding.serial, binding.transport)
            expected = {
                "serial": binding.serial,
                "transport": binding.transport,
                "ro_serialno": binding.ro_serialno,
                "build_fingerprint": binding.build_fingerprint,
                "product_device": binding.product_device,
                "api_level": binding.api_level,
                "avd_name": binding.avd_name,
                "usb_transport": binding.usb_transport,
            }
            ready = observed == expected
            reason = None if ready else "identity-unknown"
        except SessionError as error:
            ready, reason = False, error.code
        candidates.append(
            {
                "target_key": safe_key,
                "label": "Android target",
                "tags": ["android"],
                "roles": [],
                "aliases": [safe_key],
                "capabilities": sorted(binding.capabilities),
                "ready": ready,
                "reason": reason,
                "remediation": None
                if ready
                else "Check the selected host Android target configuration",
                "preparation": [],
                "rank": [0 if ready else 1],
                "binding": {"target_key": safe_key, "android": raw_binding},
            }
        )
    print(
        json.dumps(
            {
                "protocol_version": 1,
                "backend": request.backend,
                "backend_revision": request.backend_revision,
                "rank_schema": ["availability"],
                "candidates": candidates,
                "errors": [],
            },
            separators=(",", ":"),
        )
    )
    return 0


@dataclass
class ResourceReceipt:
    operation_ref: str
    fixture_id: str
    kind: str
    handle: dict[str, str]
    state: str

    def private(self) -> dict[str, object]:
        return {
            "operation_ref": self.operation_ref,
            "fixture_id": self.fixture_id,
            "kind": self.kind,
            "handle": self.handle,
            "state": self.state,
        }


class AndroidSessionOwner:
    """One parent-owned Android lifetime; observers can only request bounded work."""

    def __init__(self, context: OwnerContext, target: TargetEnvelope):
        if context.secret is None:
            raise _error("invalid", "Android session owner requires a parent context")
        self.context, self.target = context, target
        self.session_id = sha256(
            f"{target.run_id}:{context.owner_ref}:{context.generation}".encode()
        ).hexdigest()[:32]
        self.resources: list[ResourceReceipt] = []
        self.installed: OwnerRequest | None = None
        self.active = False
        self.app_cleanup_required = False
        self.cleanup_known = True
        self._app_lease: BinaryIO | None = None
        self._capture_request: OwnerRequest | None = None
        self._capture_cancel: threading.Event | None = None
        self.stop = threading.Event()
        self._lock = threading.RLock()
        self._load_journal()

    @classmethod
    def from_environ(cls, context: OwnerContext) -> "AndroidSessionOwner":
        return cls(context, TargetEnvelope.load(context))

    def _journal_path(self) -> Path:
        return self.context.private_root / "android-session.json"

    def _record(self) -> None:
        value = {
            "version": 1,
            "run_id": self.target.run_id,
            "owner_ref": self.context.owner_ref,
            "generation": self.context.generation,
            "target_fingerprint": sha256(
                (
                    self.target.binding.build_fingerprint
                    + self.target.binding.ro_serialno
                ).encode()
            ).hexdigest(),
            "resources": [entry.private() for entry in self.resources],
            "cleanup_known": self.cleanup_known,
            "app_cleanup_required": self.app_cleanup_required,
            "installed": None
            if self.installed is None
            else self.installed.install.result.to_dict()
            if self.installed.install
            else None,
        }
        _write_receipt(self.context.private_root, "android-session.json", value)

    def _load_journal(self) -> None:
        try:
            data = read_private_json(self._journal_path())
        except FileNotFoundError:
            return
        except (OSError, SessionError) as error:
            raise _error("unknown", "Android session journal is unavailable") from error
        expected = {
            "version",
            "run_id",
            "owner_ref",
            "generation",
            "target_fingerprint",
            "resources",
            "cleanup_known",
            "app_cleanup_required",
            "installed",
        }
        fingerprint = sha256(
            (
                self.target.binding.build_fingerprint + self.target.binding.ro_serialno
            ).encode()
        ).hexdigest()
        if (
            set(data) != expected
            or data["version"] != 1
            or data["run_id"] != self.target.run_id
            or data["owner_ref"] != self.context.owner_ref
            or data["generation"] != self.context.generation
            or data["target_fingerprint"] != fingerprint
            or not isinstance(data["resources"], list)
            or type(data["app_cleanup_required"]) is not bool
            or len(data["resources"]) > 64
        ):
            raise _error("unknown", "Android session journal identity is unknown")
        for raw in data["resources"]:
            receipt = strict_object(
                raw, {"operation_ref", "fixture_id", "kind", "handle", "state"}
            )
            fixture_id = _text(
                receipt["fixture_id"], pattern=_FIXTURE, field="fixture identity"
            )
            fixture = self.target.binding.fixtures.get(fixture_id)
            handle = receipt["handle"]
            state = receipt["state"]
            if (
                fixture is None
                or receipt["kind"] != fixture.kind
                or not isinstance(handle, dict)
                or set(handle) != {"local", "remote"}
                or handle != {"local": fixture.local, "remote": fixture.remote}
                or not isinstance(state, str)
                or state
                not in {"create-started", "created", "cleanup-started", "cleaned"}
            ):
                raise _error("unknown", "Android session resource receipt is unknown")
            self.resources.append(
                ResourceReceipt(
                    _text(receipt["operation_ref"], field="operation identity"),
                    fixture_id,
                    fixture.kind,
                    {"local": fixture.local, "remote": fixture.remote},
                    state,
                )
            )
        self.app_cleanup_required = bool(data["app_cleanup_required"])
        # A restarted owner can clean exact persisted handles, but never adopts a live app.
        self.cleanup_known = False

    def _revalidate(self) -> None:
        observed = probe_android(
            self.target.binding.adb,
            self.target.binding.serial,
            self.target.binding.transport,
        )
        expected = {
            "serial": self.target.binding.serial,
            "transport": self.target.binding.transport,
            "ro_serialno": self.target.binding.ro_serialno,
            "build_fingerprint": self.target.binding.build_fingerprint,
            "product_device": self.target.binding.product_device,
            "api_level": self.target.binding.api_level,
            "avd_name": self.target.binding.avd_name,
            "usb_transport": self.target.binding.usb_transport,
        }
        if observed != expected:
            raise _error("identity-unknown", "Recorded Android target identity changed")

    def _require_capability(self, name: str) -> None:
        if name not in self.target.capabilities:
            raise _error(
                "unavailable", "Selected Android target lacks the required capability"
            )

    def _require_live_app(self) -> None:
        if not self.active:
            raise _error("unknown", "Android app lifetime is not owned by this session")

    def _require_operation(self, request: OwnerRequest) -> None:
        if (
            request.source_revision != self.context.source_revision
            or request.operation not in self.target.task_keys
        ):
            raise _error("invalid", "Android operation does not match this session")
        if request.operation not in {
            "run",
            "install",
            "launch",
            "stop",
            "instrument",
            "logs",
            "fixture",
            "cleanup",
            "capture",
            "status",
        }:
            raise _error("unavailable", "Android operation is not supported")
        if request.operation not in {"cleanup", "status"}:
            self._require_capability(request.operation)

    def _safe_state(self) -> dict[str, object]:
        return {
            "state": "active" if self.active else "inactive",
            "installation": (
                {"known": False}
                if self.installed is None
                else {
                    "known": True,
                    "result_id": self.installed.install.result.result_id,
                    "artifact_id": self.installed.install.result.artifact_id,
                    "sha256": self.installed.install.result.sha256,
                }
            ),
            "resources": [
                {"fixture_id": item.fixture_id, "kind": item.kind, "state": item.state}
                for item in self.resources
            ],
        }

    def _status(self) -> dict[str, object]:
        try:
            self._revalidate()
            state = "active" if self.active else "stopped"
        except SessionError:
            state = "unknown"
        provenance = (
            {"known": False}
            if self.installed is None
            else {
                "known": True,
                "result_id": self.installed.install.result.result_id,
                "artifact_id": self.installed.install.result.artifact_id,
                "sha256": self.installed.install.result.sha256,
            }
        )
        return {
            "version": 1,
            "run_id": self.target.run_id,
            "state": state,
            "source_revision": self.context.source_revision,
            "platform": "android",
            "transport": self.target.binding.transport,
            "target_fingerprint": sha256(
                (
                    self.target.binding.build_fingerprint
                    + self.target.binding.ro_serialno
                ).encode()
            ).hexdigest(),
            "binary_provenance": provenance,
            "capabilities": sorted(self.target.capabilities),
            "resources": [
                {"fixture_id": item.fixture_id, "kind": item.kind, "state": item.state}
                for item in self.resources
            ],
        }

    def _inspect_apk(self, path: Path) -> None:
        try:
            payload = _run(
                (self.target.binding.package_inspector, str(path)), timeout=20
            )
            decoded = json.loads(payload.decode("utf-8"))
            identity = strict_object(decoded, {"package", "component"})
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            SessionError,
        ) as error:
            raise _error(
                "unavailable", "Declared Android package inspection failed"
            ) from error
        if (
            identity["package"] != self.target.binding.package
            or identity["component"] != self.target.binding.component
        ):
            raise _error(
                "invalid", "Verified Android artifact does not match selected app"
            )

    def _claim_app(self) -> None:
        if self._app_lease is None:
            self._app_lease = acquire_app_lease(
                "android", self.target.binding.serial, self.target.binding.package
            )

    def _install(self, request: OwnerRequest) -> None:
        self._claim_app()
        grant = request.install
        if grant is None:
            raise _error(
                "invalid", "Android installation requires a verified result grant"
            )
        if file_digest(grant.path, size=grant.size) != grant.result.sha256:
            raise _error("unknown", "Verified Android artifact identity changed")
        self._inspect_apk(grant.path)
        output = _adb(
            self.target.binding.adb,
            self.target.binding.serial,
            "install",
            "-r",
            str(grant.path),
            timeout=90,
        )
        if "Success" not in output.decode("utf-8", "replace"):
            raise _error("unhealthy", "Android installation was not acknowledged")
        resolved = _adb(
            self.target.binding.adb,
            self.target.binding.serial,
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            self.target.binding.component,
        )
        if self.target.binding.component not in resolved.decode("utf-8", "replace"):
            raise _error(
                "unhealthy", "Installed Android app identity was not acknowledged"
            )
        self.installed = request
        self._record()

    def _launch(self) -> None:
        self._claim_app()
        self.active = True
        self.app_cleanup_required = True
        self._record()
        output = _adb(
            self.target.binding.adb,
            self.target.binding.serial,
            "shell",
            "am",
            "start",
            "-W",
            "-n",
            self.target.binding.component,
        )
        if (
            "Status: ok" not in output.decode("utf-8", "replace")
            or foreground_android(self.target.binding.adb, self.target.binding.serial)
            != self.target.binding.package
        ):
            raise _error("unhealthy", "Android app launch was not acknowledged")
        self._record()

    def _app_is_stopped(self) -> bool:
        pids = _adb(
            self.target.binding.adb,
            self.target.binding.serial,
            "shell",
            "pidof",
            self.target.binding.package,
            accepted_exit_codes=(0, 1),
        ).strip()
        return (
            not pids
            and foreground_android(self.target.binding.adb, self.target.binding.serial)
            != self.target.binding.package
        )

    def _stop_owned(self) -> None:
        if self._app_lease is None:
            # A journal is not a live app lease. Recovery may acknowledge absence,
            # but must never stop an app now belonging to another owner.
            if not self._app_is_stopped():
                raise _error("unknown", "Android app lifetime ownership is unavailable")
        else:
            _adb(
                self.target.binding.adb,
                self.target.binding.serial,
                "shell",
                "am",
                "force-stop",
                self.target.binding.package,
            )
            if not self._app_is_stopped():
                raise _error("unhealthy", "Android app stop was not acknowledged")
        self.active = False
        self.app_cleanup_required = False
        self._record()

    def _stop(self) -> None:
        self._require_live_app()
        self._stop_owned()

    def _instrument(self) -> None:
        instrumentation = self.target.options.instrumentation
        if instrumentation is None:
            raise _error(
                "unavailable", "Selected Android profile has no instrumentation"
            )
        output = _adb(
            self.target.binding.adb,
            self.target.binding.serial,
            "shell",
            "am",
            "instrument",
            "-w",
            instrumentation,
            timeout=90,
        )
        lines = output.decode("utf-8", "replace").strip().splitlines()
        statuses = [
            line.partition(":")[2].strip()
            for line in lines
            if line.startswith("INSTRUMENTATION_STATUS_CODE:")
        ]
        if (
            not lines
            or lines[-1] != "INSTRUMENTATION_CODE: -1"
            or not statuses
            or any(code not in {"-4", "-3", "0", "1", "2"} for code in statuses)
        ):
            raise _error("unhealthy", "Android instrumentation did not report success")

    def _logs(
        self, cancel: threading.Event, emit: Callable[[dict[str, object]], None]
    ) -> None:
        self._require_live_app()
        raw_pids = (
            _adb(
                self.target.binding.adb,
                self.target.binding.serial,
                "shell",
                "pidof",
                self.target.binding.package,
                cancel=cancel,
            )
            .decode("utf-8", "replace")
            .split()
        )
        if not raw_pids or not all(item.isdecimal() for item in raw_pids):
            raise _error("unhealthy", "Selected Android app has no loggable process")
        output = _adb(
            self.target.binding.adb,
            self.target.binding.serial,
            "logcat",
            "-d",
            "--pid=" + ",".join(raw_pids),
            cancel=cancel,
            timeout=30,
        )
        for line in output.decode("utf-8", "replace").splitlines()[:1000]:
            line = re.sub(r"(?:https?|wss?)://\S+", "[redacted-url]", line)
            line = re.sub(r"(?i)(token|secret|password)=\S+", r"\1=[redacted]", line)
            line = line.replace(self.target.binding.serial, "[redacted-device]")
            line = line.replace(self.target.binding.adb, "[redacted-path]")
            line = line.replace(
                self.target.binding.package_inspector, "[redacted-path]"
            )
            for pid in raw_pids:
                line = re.sub(rf"\b{re.escape(pid)}\b", "[redacted-pid]", line)
            if line:
                emit({"kind": "log", "message": line[:2048]})

    def _fixture_mapping_state(self, kind: str, handle: Mapping[str, str]) -> str:
        """Return exact, absent, or unsafe for this selected target's endpoint."""
        listing = _adb(
            self.target.binding.adb,
            self.target.binding.serial,
            kind,
            "--list",
        )
        mappings = _fixture_mappings(
            kind,
            self.target.binding.serial,
            listing,
        )
        local, remote = handle["local"], handle["remote"]
        same_endpoint = [
            mapping
            for mapping in mappings
            if (mapping[1] == local if kind == "forward" else mapping[2] == remote)
        ]
        if not same_endpoint:
            return "absent"
        expected = (self.target.binding.serial, local, remote)
        if len(same_endpoint) == 1 and same_endpoint[0] == expected:
            return "exact"
        return "unsafe"

    def _fixture(self, request: OwnerRequest, payload: dict[str, object]) -> None:
        data = strict_object(payload, {"fixture_id"})
        fixture_id = _text(
            data["fixture_id"], pattern=_FIXTURE, field="fixture identity"
        )
        fixture = self.target.binding.fixtures.get(fixture_id)
        if fixture is None:
            raise _error("unavailable", "Requested Android fixture is not configured")
        if any(
            item.fixture_id == fixture_id and item.state == "created"
            for item in self.resources
        ):
            raise _error("invalid", "Android fixture is already owned by this session")
        handle = {"local": fixture.local, "remote": fixture.remote}
        if self._fixture_mapping_state(fixture.kind, handle) != "absent":
            raise _error(
                "unavailable",
                "Configured Android fixture endpoint is already in use",
            )
        receipt = ResourceReceipt(
            request.operation_ref,
            fixture_id,
            fixture.kind,
            handle,
            "create-started",
        )
        self.resources.append(receipt)
        self._record()
        try:
            _adb(
                self.target.binding.adb,
                self.target.binding.serial,
                fixture.kind,
                "--no-rebind",
                fixture.local if fixture.kind == "forward" else fixture.remote,
                fixture.remote if fixture.kind == "forward" else fixture.local,
            )
        except SessionError:
            # The no-rebind failure may race after the preflight. It cannot establish
            # which resource, if any, is ours, so recovery must not remove either one.
            self.cleanup_known = False
            self._record()
            raise
        receipt.state = "created"
        self._record()

    def _cleanup(self) -> None:
        self.cleanup_known = False
        self._record()
        self._revalidate()
        for receipt in self.resources:
            if receipt.state == "cleaned":
                continue
            if receipt.state != "created":
                raise _error("unknown", "Android resource cleanup state is unknown")
            state = self._fixture_mapping_state(receipt.kind, receipt.handle)
            if state == "absent":
                receipt.state = "cleaned"
                self._record()
                continue
            if state != "exact":
                raise _error("unknown", "Android fixture mapping identity is unknown")
            receipt.state = "cleanup-started"
            self._record()
            _adb(
                self.target.binding.adb,
                self.target.binding.serial,
                receipt.kind,
                "--remove",
                receipt.handle["local"]
                if receipt.kind == "forward"
                else receipt.handle["remote"],
            )
            if self._fixture_mapping_state(receipt.kind, receipt.handle) != "absent":
                raise _error("unknown", "Android fixture removal is not acknowledged")
            receipt.state = "cleaned"
            self._record()
        self.cleanup_known = True
        self._record()

    def shutdown(self) -> bool:
        """Stop only a receipt-backed app launch and exact recorded resources."""
        with self._lock:
            try:
                self._revalidate()
                if self.app_cleanup_required:
                    self._stop_owned()
                self._cleanup()
            except SessionError:
                self.cleanup_known = False
                self._record()
            finally:
                if self._app_lease is not None:
                    self._app_lease.close()
                    self._app_lease = None
            return self.cleanup_known

    def observe_capture(
        self,
        session_id: str,
        capture_task_key: str,
        kinds: tuple[str, ...],
        platform: str,
    ) -> dict[str, object]:
        with self._lock:
            request = self._capture_request
            if (
                session_id != self.session_id
                or request is None
                or request.capture is None
            ):
                raise _error("unknown", "Android capture session is unavailable")
            if platform != "android" or capture_task_key != self.target.task_keys.get(
                "capture"
            ):
                raise _error(
                    "invalid", "Android capture does not match configured session task"
                )
            if kinds != request.capture.kinds or platform != request.capture.platform:
                raise _error(
                    "invalid", "Android capture does not match trusted capability"
                )
            self._require_live_app()
            self._require_capability("capture")
            self._revalidate()
            before = foreground_android(
                self.target.binding.adb, self.target.binding.serial
            )
            if before != self.target.binding.package:
                raise _error("unhealthy", "Selected Android app is not foreground")
            capture_android(
                self.target.binding.adb,
                self.target.binding.serial,
                request.capture.directory,
                kinds,
                self._capture_cancel
                if self._capture_cancel is not None
                else threading.Event(),
            )
            after = foreground_android(
                self.target.binding.adb, self.target.binding.serial
            )
            expected = [
                request.capture.directory
                / ("screen.png" if kind == "image" else "layout.xml")
                for kind in kinds
            ]
            if after != self.target.binding.package or any(
                not path.is_file() or path.stat().st_size == 0 for path in expected
            ):
                raise _error(
                    "unhealthy",
                    "Android capture did not preserve selected app identity",
                )
            return {"captured": list(kinds), "state": "completed"}

    def handle(
        self,
        request: OwnerRequest,
        payload: dict[str, object],
        cancel: threading.Event,
        emit: Callable[[dict[str, object]], None],
    ) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise _error("invalid", "Invalid Android operation input")
        if request.operation == "logs":
            with self._lock:
                self._require_operation(request)
                strict_object(payload, set())
                if cancel.is_set():
                    raise _error("cancelled", "Android operation was cancelled")
                self._revalidate()
                self._require_live_app()
            self._logs(cancel, emit)
            with self._lock:
                return self._safe_state()
        with self._lock:
            self._require_operation(request)
            if cancel.is_set():
                raise _error("cancelled", "Android operation was cancelled")
            try:
                operation = request.operation
                if operation == "status":
                    strict_object(payload, set())
                    return self._status()
                if operation != "cleanup":
                    self._revalidate()
                if operation in {"run", "install"}:
                    strict_object(payload, set())
                    if request.install is not None:
                        self._install(request)
                    elif operation == "install":
                        raise _error(
                            "invalid",
                            "Android installation requires a verified result grant",
                        )
                    if operation == "run":
                        self._launch()
                elif operation == "launch":
                    strict_object(payload, set())
                    self._launch()
                elif operation == "stop":
                    strict_object(payload, set())
                    self._stop()
                elif operation == "instrument":
                    strict_object(payload, set())
                    self._instrument()
                elif operation == "fixture":
                    self._fixture(request, payload)
                elif operation == "cleanup":
                    strict_object(payload, set())
                    if self.app_cleanup_required:
                        self._stop_owned()
                    self._cleanup()
                elif operation == "capture":
                    strict_object(payload, set())
                    if request.capture is None:
                        raise _error(
                            "invalid", "Android capture requires a trusted capability"
                        )
                    self._capture_request, self._capture_cancel = request, cancel
                    try:
                        return self.observe_capture(
                            self.session_id,
                            self.target.task_keys.get("capture", ""),
                            request.capture.kinds,
                            request.capture.platform,
                        )
                    finally:
                        self._capture_request, self._capture_cancel = None, None
                return self._safe_state()
            except SessionError:
                if request.operation == "cleanup":
                    self.cleanup_known = False
                    self._record()
                raise


def _operation_payload() -> dict[str, object]:
    value = os.environ.get(SESSION_OPERATION_FILE)
    if value is None:
        return {}
    try:
        return read_private_json(Path(value))
    except (OSError, SessionError) as error:
        raise _error("invalid", "Invalid private Android operation input") from error


def main() -> int:
    context = OwnerContext.from_environ()
    payload = _operation_payload()
    emit = lambda event: print(json.dumps(event, separators=(",", ":")), flush=True)
    if context.secret is None:
        if context.request is None:
            raise _error("invalid", "Android observer is missing its trusted request")
        result = OwnerClient(context).call(
            context.request.operation, payload, event_sink=emit
        )
        print(json.dumps(result, separators=(",", ":")), flush=True)
        return 0
    owner = AndroidSessionOwner.from_environ(context)
    request = context.request
    if request is None:
        raise _error("invalid", "Android parent is missing its trusted request")
    if owner.target.operation != "run" or request.operation != "run":
        raise _error("invalid", "Primary Android owner invocation must be run")
    previous = {
        sig: signal.signal(sig, lambda *_: owner.stop.set())
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    cleanup_known = False
    try:
        context.begin()
        result = owner.handle(request, payload, owner.stop, emit)
        print(json.dumps(result, separators=(",", ":")), flush=True)
        serve_owner(context, owner.handle, owner.stop)
        return 0
    finally:
        try:
            cleanup_known = owner.shutdown()
        finally:
            try:
                context.finish(cleanup_known=cleanup_known)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["discover"]:
            raise SystemExit(discover_main())
        if len(sys.argv) != 1:
            raise _error("invalid", "Invalid Android adapter invocation")
        raise SystemExit(main())
    except SessionError as error:
        print(
            json.dumps({"status": "error", "code": error.code}, separators=(",", ":"))
        )
        raise SystemExit(2)
    except Exception:
        print(json.dumps({"status": "error", "code": "unknown"}, separators=(",", ":")))
        raise SystemExit(2)
