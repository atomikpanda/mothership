#!/usr/bin/env python3
"""Read-only-discovery, target-pinned native iOS simulator backend example."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import emit_inventory, load_bindings, load_context, load_request  # noqa: E402
from mship.core.session_channel import OwnerContext, acquire_app_lease
from mship.core.session_inputs import SessionError

_LIMIT = 1024 * 1024
_UUID = re.compile(r"^[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,512}$")
_ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_BUNDLE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_VERSION = re.compile(r"(?:iOS[- ]?)?(\d+)(?:\.(\d+)|-(\d+))?(?:\.(\d+)|-(\d+))?")


class BackendError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _fail(code: str, message: str) -> BackendError:
    return BackendError(code, message)


def _run(argv: Sequence[str], *, timeout: float = 20.0) -> bytes:
    """Collect one tool's combined output under a cap, terminating on overflow."""
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=False,
        )
    except OSError as error:
        raise _fail("tool-unavailable", "Configured iOS tool is unavailable") from error
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        assert proc.stdout is not None and proc.stderr is not None
        with selectors.DefaultSelector() as streams:
            streams.register(proc.stdout, selectors.EVENT_READ)
            streams.register(proc.stderr, selectors.EVENT_READ)
            while streams.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _fail(
                        "tool-unavailable", "Configured iOS tool did not complete"
                    )
                for key, _event in streams.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, min(65536, _LIMIT + 1 - len(output)))
                    if not chunk:
                        streams.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > _LIMIT:
                            raise _fail(
                                "tool-unavailable",
                                "Configured iOS tool returned excessive output",
                            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _fail("tool-unavailable", "Configured iOS tool did not complete")
        if proc.wait(timeout=remaining):
            raise _fail("target-unavailable", "Configured iOS target operation failed")
        return bytes(output)
    except subprocess.TimeoutExpired as error:
        raise _fail(
            "tool-unavailable", "Configured iOS tool did not complete"
        ) from error
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _foreground(argv: Sequence[str], *, timeout: float | None = None) -> None:
    try:
        result = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            start_new_session=False,
        )
    except FileNotFoundError as error:
        raise _fail("tool-unavailable", "Configured iOS tool is unavailable") from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _fail(
            "target-unavailable", "Configured iOS target operation failed"
        ) from error
    if result.returncode:
        raise _fail("target-unavailable", "Configured iOS target operation failed")


def _json(payload: bytes, message: str) -> Mapping[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise _fail("inventory-invalid", message) from error
    if not isinstance(value, dict):
        raise _fail("inventory-invalid", message)
    return value


def _runtime_rank(runtime: Mapping[str, object]) -> tuple[int, int, int] | None:
    source = (
        runtime.get("version")
        if isinstance(runtime.get("version"), str)
        else runtime.get("identifier")
    )
    if not isinstance(source, str) or (match := _VERSION.search(source)) is None:
        return None
    try:
        return (
            int(match.group(1)),
            int(match.group(2) or match.group(3) or 0),
            int(match.group(4) or match.group(5) or 0),
        )
    except ValueError:
        return None


def _available(value: Mapping[str, object]) -> bool:
    available = value.get("isAvailable")
    if isinstance(available, bool):
        return available
    state = value.get("availability")
    return isinstance(state, str) and "unavailable" not in state.lower()


def _target_key(uuid: str) -> str:
    return "sim-" + hashlib.sha256(uuid.encode()).hexdigest()[:24]


def _path_identity(value: object) -> tuple[str, int, int] | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    if not path.is_dir():
        return None
    return (str(path), info.st_dev, info.st_ino)


def _fingerprint(
    *, uuid: str, runtime: str, device_type: str, path: str, device: int, inode: int
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "uuid": uuid,
                "runtime": runtime,
                "device_type": device_type,
                "data_path": path,
                "st_dev": device,
                "st_ino": inode,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _binding(
    uuid: str, runtime: str, device: Mapping[str, object], bundle_id: str
) -> dict[str, str] | None:
    device_type = device.get("deviceTypeIdentifier")
    if not isinstance(device_type, str) or _TOKEN.fullmatch(device_type) is None:
        return None
    path_identity = _path_identity(device.get("dataPath"))
    if path_identity is None:
        return None
    path, st_dev, st_ino = path_identity
    return {
        "uuid": uuid,
        "runtime": runtime,
        "device_type": device_type,
        "data_path": path,
        "instance_fingerprint": _fingerprint(
            uuid=uuid,
            runtime=runtime,
            device_type=device_type,
            path=path,
            device=st_dev,
            inode=st_ino,
        ),
        "bundle_id": bundle_id,
    }


def _configured(
    bindings: Mapping[str, object],
) -> tuple[str, str, frozenset[str] | None, dict[str, str], tuple[str, ...] | None]:
    paths, aliases = bindings.get("paths"), bindings.get("aliases")
    if (
        not isinstance(paths, dict)
        or not isinstance(aliases, dict)
        or set(paths) - {"xcrun", "bundle_id", "allowed_uuids", "usb_inventory_argv"}
    ):
        raise _fail("invalid-binding", "Invalid iOS host bindings")
    xcrun, bundle = paths.get("xcrun"), paths.get("bundle_id")
    if (
        not isinstance(xcrun, str)
        or not Path(xcrun).is_absolute()
        or ".." in Path(xcrun).parts
        or "\x00" in xcrun
    ):
        raise _fail("invalid-binding", "Invalid iOS xcrun path")
    if not isinstance(bundle, str) or _BUNDLE.fullmatch(bundle) is None:
        raise _fail("invalid-binding", "Invalid iOS bundle identifier")
    raw_allowed = paths.get("allowed_uuids")
    if raw_allowed is None:
        allowed = None
    elif isinstance(raw_allowed, list) and all(
        isinstance(item, str) and _UUID.fullmatch(item) for item in raw_allowed
    ):
        allowed = frozenset(raw_allowed)
    else:
        raise _fail("invalid-binding", "Invalid iOS simulator allow-list")
    alias_map: dict[str, str] = {}
    for alias, uuid in aliases.items():
        if (
            not isinstance(alias, str)
            or _ALIAS.fullmatch(alias) is None
            or not isinstance(uuid, str)
            or _UUID.fullmatch(uuid) is None
        ):
            raise _fail("invalid-binding", "Invalid iOS simulator alias")
        alias_map[alias] = uuid
    raw_usb = paths.get("usb_inventory_argv")
    if raw_usb is None:
        usb = None
    elif (
        isinstance(raw_usb, list)
        and 1 <= len(raw_usb) <= 32
        and all(
            isinstance(item, str) and item and "\x00" not in item for item in raw_usb
        )
        and Path(raw_usb[0]).is_absolute()
        and ".." not in Path(raw_usb[0]).parts
    ):
        usb = tuple(raw_usb)
    else:
        raise _fail("invalid-binding", "Invalid iOS USB inventory tool")
    return xcrun, bundle, allowed, alias_map, usb


def _inventory(xcrun: str) -> Mapping[str, object]:
    return _json(
        _run((xcrun, "simctl", "list", "--json")),
        "Configured iOS simulator inventory is invalid",
    )


def _candidate(
    *,
    xcrun: str,
    uuid: str,
    runtime: str,
    device: Mapping[str, object],
    rank: tuple[int, int, int],
    bundle_id: str,
    aliases: Mapping[str, str],
) -> dict[str, object]:
    binding = _binding(uuid, runtime, device, bundle_id)
    reason = "identity-unknown" if binding is None else None
    if binding is not None:
        try:
            _app(xcrun, binding)
        except BackendError:
            reason = "app-unavailable"
    ready = reason is None
    return {
        "target_key": _target_key(uuid),
        "label": "iOS simulator",
        "tags": ["ios", "simulator"],
        "roles": [],
        "aliases": sorted(alias for alias, value in aliases.items() if value == uuid),
        "capabilities": ["run", "logs", "capture"] if ready else [],
        "ready": ready,
        "reason": reason,
        "remediation": None
        if ready
        else (
            "Install the configured app on the selected simulator"
            if reason == "app-unavailable"
            else "Configure an installed simulator with a stable data-path identity"
        ),
        "preparation": [],
        "rank": list(rank),
        "binding": (
            {
                "target_key": _target_key(uuid),
                "ios": binding,
                "platform": "ios",
                "capture_kinds": ["image"],
            }
            if binding
            else {"transport": "simulator"}
        ),
    }


def _usb_candidate(reason: str) -> dict[str, object]:
    return {
        "target_key": "ios-usb-inventory",
        "label": "Physical iOS device",
        "tags": ["ios", "usb"],
        "roles": [],
        "aliases": [],
        "capabilities": [],
        "ready": False,
        "reason": reason,
        "remediation": "Configure an approved native iOS USB operation before selecting a physical device",
        "preparation": [],
        "rank": [0, 0, 0],
        "binding": {"transport": "usb"},
    }


def _usb(argv: tuple[str, ...] | None) -> dict[str, object]:
    if argv is None:
        return _usb_candidate("usb-inventory-unconfigured")
    try:
        devices = _json(_run(argv), "Native iOS USB inventory is invalid").get(
            "devices"
        )
    except BackendError as error:
        return _usb_candidate(
            "usb-tool-unavailable"
            if error.code == "tool-unavailable"
            else "usb-inventory-unavailable"
        )
    return _usb_candidate(
        "usb-operations-unavailable"
        if isinstance(devices, list) and devices
        else "usb-unavailable"
    )


def discover(request: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    xcrun, bundle, allowed, aliases, usb = _configured(bindings)
    try:
        listing = _inventory(xcrun)
    except BackendError as error:
        emit_inventory(
            request,
            [],
            ("ios_major", "ios_minor", "ios_patch"),
            (
                {
                    "code": error.code,
                    "message": "Configured iOS simulator inventory is unavailable",
                    "remediation": "Install or repair configured Xcode command-line tools without rerunning discovery",
                },
            ),
        )
        return
    runtimes, devices = listing.get("runtimes"), listing.get("devices")
    candidates: list[dict[str, object]] = []
    if isinstance(runtimes, list) and isinstance(devices, dict):
        for runtime_info in runtimes:
            if not isinstance(runtime_info, dict) or not _available(runtime_info):
                continue
            runtime, rank = runtime_info.get("identifier"), _runtime_rank(runtime_info)
            if (
                not isinstance(runtime, str)
                or rank is None
                or not isinstance(devices.get(runtime), list)
            ):
                continue
            for device in devices[runtime]:
                uuid = device.get("udid") if isinstance(device, dict) else None
                if (
                    not isinstance(uuid, str)
                    or _UUID.fullmatch(uuid) is None
                    or not _available(device)
                ):
                    continue
                if allowed is not None and uuid not in allowed:
                    continue
                candidates.append(
                    _candidate(
                        xcrun=xcrun,
                        uuid=uuid,
                        runtime=runtime,
                        device=device,
                        rank=rank,
                        bundle_id=bundle,
                        aliases=aliases,
                    )
                )
    candidates.append(_usb(usb))
    emit_inventory(request, candidates, ("ios_major", "ios_minor", "ios_patch"))


def _selected(
    request: Mapping[str, object], bindings: Mapping[str, object]
) -> tuple[str, dict[str, str]]:
    xcrun, bundle, allowed, _aliases, _usb_argv = _configured(bindings)
    private = load_context().get("private_binding")
    if (
        not isinstance(private, dict)
        or set(private) != {"target_key", "ios", "platform", "capture_kinds"}
        or private.get("platform") != "ios"
        or private.get("capture_kinds") != ["image"]
    ):
        raise _fail("identity-lost", "Selected iOS simulator context is unavailable")
    raw = private.get("ios")
    if not isinstance(raw, dict) or set(raw) != {
        "uuid",
        "runtime",
        "device_type",
        "data_path",
        "instance_fingerprint",
        "bundle_id",
    }:
        raise _fail("identity-lost", "Selected iOS simulator context is unavailable")
    if (
        any(not isinstance(value, str) for value in raw.values())
        or raw["bundle_id"] != bundle
        or _UUID.fullmatch(raw["uuid"]) is None
    ):
        raise _fail("identity-lost", "Selected iOS simulator identity changed")
    if allowed is not None and raw["uuid"] not in allowed:
        raise _fail("identity-lost", "Selected iOS simulator is no longer allowed")
    listing = _inventory(xcrun)
    runtime_devices = listing.get("devices")
    if not isinstance(runtime_devices, dict) or not isinstance(
        runtime_devices.get(raw["runtime"]), list
    ):
        raise _fail("identity-lost", "Selected iOS simulator is unavailable")
    device = next(
        (
            item
            for item in runtime_devices[raw["runtime"]]
            if isinstance(item, dict) and item.get("udid") == raw["uuid"]
        ),
        None,
    )
    observed = (
        None
        if not isinstance(device, dict) or not _available(device)
        else _binding(raw["uuid"], raw["runtime"], device, bundle)
    )
    if observed != raw:
        raise _fail("identity-lost", "Selected iOS simulator identity changed")
    return xcrun, dict(raw)


def _app(xcrun: str, binding: Mapping[str, str]) -> None:
    _run(
        (
            xcrun,
            "simctl",
            "get_app_container",
            binding["uuid"],
            binding["bundle_id"],
            "app",
        )
    )


def _capture_directory() -> Path:
    value = os.environ.get("MSHIP_CAPTURE_DIR")
    directory = Path(value) if value else None
    if (
        directory is None
        or not directory.is_absolute()
        or ".." in directory.parts
        or not directory.is_dir()
    ):
        raise _fail("invalid", "iOS capture output directory is unavailable")
    if set(filter(None, os.environ.get("MSHIP_CAPTURE_KINDS", "").split(","))) != {
        "image"
    }:
        raise _fail("unsupported", "Native iOS simulator capture supports image only")
    if os.environ.get("MSHIP_CAPTURE_PLATFORM") not in {None, "ios"}:
        raise _fail("invalid", "iOS capture platform does not match selected target")
    return directory


def _assert_app_absent(
    xcrun: str, binding: Mapping[str, str], *, timeout: float = 20.0
) -> None:
    """Refuse to adopt or later terminate a pre-existing simulator app."""
    try:
        listing = _run(
            (xcrun, "simctl", "spawn", binding["uuid"], "launchctl", "list"),
            timeout=timeout,
        ).decode("utf-8", "strict")
    except (BackendError, UnicodeError) as error:
        raise _fail(
            "owner-unavailable",
            "iOS simulator cannot prove the configured app is absent",
        ) from error
    if binding["bundle_id"] in listing:
        raise _fail("busy", "The selected app is already running")


def _launch_pid(xcrun: str, binding: Mapping[str, str]) -> int:
    output = _run((xcrun, "simctl", "launch", binding["uuid"], binding["bundle_id"]))
    try:
        bundle_id, separator, value = output.decode("ascii").strip().partition(": ")
    except UnicodeDecodeError as error:
        raise _fail(
            "identity-lost", "iOS simulator launch did not acknowledge an app identity"
        ) from error
    if (
        not separator
        or bundle_id != binding["bundle_id"]
        or not value.isdecimal()
        or not 0 < int(value) <= 2**31 - 1
    ):
        raise _fail(
            "identity-lost", "iOS simulator launch did not acknowledge an app identity"
        )
    return int(value)


def _is_owned_launch(xcrun: str, binding: Mapping[str, str], pid: int) -> bool:
    """Ask selected simulator launchd about the exact app process this run created."""
    try:
        receipt = _run(
            (
                xcrun,
                "simctl",
                "spawn",
                binding["uuid"],
                "launchctl",
                "procinfo",
                str(pid),
            ),
            timeout=4.0,
        ).decode("utf-8", "strict")
    except BackendError, UnicodeError:
        return False
    return binding["bundle_id"] in receipt and str(pid) in receipt


def _run_lifetime(xcrun: str, binding: Mapping[str, str]) -> None:
    """Own one exact simulator launch until it exits or this tool is cancelled."""
    try:
        owner = OwnerContext.from_environ()
        lease = acquire_app_lease("ios", binding["uuid"], binding["bundle_id"])
    except SessionError as error:
        raise _fail(error.code, str(error)) from error
    stop = False
    previous: dict[int, object] = {}
    cleanup_known = True

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    try:
        owner.begin()
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        # Refusal here has not launched or mutated an existing app.
        _assert_app_absent(xcrun, binding)
        cleanup_known = False
        pid = _launch_pid(xcrun, binding)
        if not _is_owned_launch(xcrun, binding, pid):
            raise _fail(
                "identity-lost", "iOS simulator launch ownership cannot be verified"
            )
        owner.ready()
        while not stop and _is_owned_launch(xcrun, binding, pid):
            time.sleep(0.2)
        if stop and _is_owned_launch(xcrun, binding, pid):
            _run(
                (xcrun, "simctl", "terminate", binding["uuid"], binding["bundle_id"]),
                timeout=4.0,
            )
        try:
            _assert_app_absent(xcrun, binding, timeout=4.0)
            cleanup_known = True
        except BackendError:
            cleanup_known = False
    finally:
        try:
            owner.finish(cleanup_known=cleanup_known)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            lease.close()


def run(request: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    xcrun, binding = _selected(request, bindings)
    _app(xcrun, binding)
    _run_lifetime(xcrun, binding)


def logs(request: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    xcrun, binding = _selected(request, bindings)
    _app(xcrun, binding)
    _foreground(
        (
            xcrun,
            "simctl",
            "spawn",
            binding["uuid"],
            "log",
            "stream",
            "--style",
            "compact",
            "--predicate",
            f'process == "{binding["bundle_id"]}"',
        )
    )


def capture(request: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    xcrun, binding = _selected(request, bindings)
    _app(xcrun, binding)
    destination = _capture_directory() / "screen.png"
    _foreground(
        (xcrun, "simctl", "io", binding["uuid"], "screenshot", str(destination)),
        timeout=30.0,
    )
    try:
        if (
            destination.stat().st_size < 8
            or destination.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n"
        ):
            raise _fail("target-unavailable", "iOS simulator screenshot was empty")
    except OSError as error:
        raise _fail(
            "target-unavailable", "iOS simulator screenshot was unavailable"
        ) from error


def main(argv: Sequence[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] != "discover"):
        print("usage: backend.py [discover]", file=sys.stderr)
        return 2
    try:
        request, bindings = load_request(), load_bindings()
        action = "discover" if len(argv) == 2 else request.get("operation")
        if action == "discover":
            discover(request, bindings)
        elif action == "run":
            run(request, bindings)
        elif action == "logs":
            logs(request, bindings)
        elif action == "capture":
            capture(request, bindings)
        else:
            raise _fail("invalid", "Requested iOS operation is unavailable")
    except BackendError as error:
        print(f"iOS backend {error.code}: {error}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "iOS backend invalid: configured iOS backend input is unavailable",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
