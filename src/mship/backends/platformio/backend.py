#!/usr/bin/env python3
"""Read-only PlatformIO inventory with an explicit, target-pinned monitor/upload split."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import pty
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO, NoReturn

from mship.backends.common import (
    ExampleError,
    emit_inventory,
    load_bindings,
    load_context,
    load_request,
)
from mship.core.session_channel import OwnerContext
from mship.core.session_inputs import SessionError

_LIMIT = 256 * 1024
_ENVIRONMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HEX = re.compile(r"^[0-9A-Fa-f]{4}$")
_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_HWID_VID_PID = re.compile(r"(?:USB\s+)?VID:PID=([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})")
_HWID_SERIAL = re.compile(r"(?:^|\s)SER=([^\s]+)")
_RETAINED_LOG_BYTES = 512 * 1024


class BackendError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _fail(code: str, message: str) -> NoReturn:
    raise BackendError(code, message)


def _run(argv: Sequence[str], *, cwd: Path) -> bytes:
    """Execute only bounded, read-only PlatformIO inventory commands."""
    try:
        result = subprocess.run(
            tuple(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("MSHIP_")
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _fail("tool-unavailable", "Configured PlatformIO inventory is unavailable")
    if result.returncode != 0 or len(result.stdout) > _LIMIT:
        _fail("inventory-unavailable", "PlatformIO read-only inventory is unavailable")
    return result.stdout


def _json(payload: bytes, *, label: str) -> object:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackendError(
            "inventory-malformed", f"PlatformIO {label} inventory is malformed"
        ) from error


def _configured(bindings: Mapping[str, object]) -> tuple[str, Path, Path]:
    paths = bindings.get("paths")
    value = paths.get("platformio") if isinstance(paths, Mapping) else None
    if not isinstance(value, Mapping) or set(value) != {
        "executable",
        "project_dir",
        "monitor_state_dir",
    }:
        _fail("configuration-unavailable", "PlatformIO host bindings are unavailable")
    executable, project, state = (
        value["executable"],
        value["project_dir"],
        value["monitor_state_dir"],
    )
    if (
        not isinstance(executable, str)
        or not os.path.isabs(executable)
        or not os.access(executable, os.X_OK)
    ):
        _fail("tool-unavailable", "Configured PlatformIO executable is unavailable")
    if not isinstance(project, str) or not os.path.isabs(project):
        _fail(
            "configuration-unavailable", "Configured PlatformIO project is unavailable"
        )
    project_directory = Path(project)
    if not project_directory.is_dir():
        _fail(
            "configuration-unavailable", "Configured PlatformIO project is unavailable"
        )
    if not isinstance(state, str) or not os.path.isabs(state):
        _fail(
            "configuration-unavailable",
            "Configured PlatformIO monitor state directory is unavailable",
        )
    state_directory = Path(state)
    try:
        info = state_directory.stat()
    except OSError as error:
        raise BackendError(
            "configuration-unavailable",
            "Configured PlatformIO monitor state directory is unavailable",
        ) from error
    if (
        not state_directory.is_dir()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        _fail(
            "configuration-unavailable",
            "Configured PlatformIO monitor state directory is not private",
        )
    return executable, project_directory, state_directory


def _environment_metadata(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        _fail(
            "project-config-malformed",
            "PlatformIO project environment configuration is malformed",
        )
    environments: dict[str, dict[str, str]] = {}
    for section, raw in value.items():
        if (
            not isinstance(section, str)
            or not section.startswith("env:")
            or not isinstance(raw, Mapping)
        ):
            continue
        name = section.removeprefix("env:")
        platform, board = raw.get("platform"), raw.get("board")
        if isinstance(platform, list) and len(platform) == 1:
            platform = platform[0]
        if isinstance(board, list) and len(board) == 1:
            board = board[0]
        if (
            not _ENVIRONMENT.fullmatch(name)
            or not isinstance(platform, str)
            or not isinstance(board, str)
        ):
            continue
        if not _ENVIRONMENT.fullmatch(platform) or not _ENVIRONMENT.fullmatch(board):
            continue
        environments[name] = {"name": name, "platform": platform, "board": board}
    if not environments:
        _fail(
            "project-config-incomplete",
            "PlatformIO project has no configured board environments",
        )
    return environments


def _requested_environments(
    request: Mapping[str, object], environments: Mapping[str, dict[str, str]]
) -> tuple[dict[str, str], ...]:
    options = request.get("options")
    if not isinstance(options, Mapping) or set(options) - {"environment"}:
        _fail(
            "invalid", "PlatformIO profile options support only an optional environment"
        )
    selected = options.get("environment")
    if selected is None:
        return tuple(environments[name] for name in sorted(environments))
    if (
        not isinstance(selected, str)
        or not _ENVIRONMENT.fullmatch(selected)
        or selected not in environments
    ):
        _fail(
            "environment-unavailable", "Requested PlatformIO environment is unavailable"
        )
    return (environments[selected],)


def _physical(device: object) -> dict[str, str] | None:
    if not isinstance(device, Mapping):
        return None
    vid = device.get("vid")
    pid = device.get("pid")
    hwid = device.get("hwid")
    if not isinstance(vid, str) or not isinstance(pid, str):
        match = _HWID_VID_PID.search(hwid) if isinstance(hwid, str) else None
        if match is None:
            return None
        vid, pid = match.groups()
    serial = device.get("serial")
    if not isinstance(serial, str):
        match = _HWID_SERIAL.search(hwid) if isinstance(hwid, str) else None
        serial = match.group(1) if match is not None else None
    if (
        not isinstance(serial, str)
        or not _HEX.fullmatch(vid)
        or not _HEX.fullmatch(pid)
        or not _SERIAL.fullmatch(serial)
    ):
        return None
    return {"vid": vid.upper(), "pid": pid.upper(), "serial": serial}


def _physical_key(physical: Mapping[str, str]) -> str:
    return f"{physical['vid']}:{physical['pid']}:{physical['serial']}"


def _target_key(physical: Mapping[str, str], environment: Mapping[str, str]) -> str:
    payload = _physical_key(physical) + "\0" + environment["name"]
    return "platformio-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _aliases(
    bindings: Mapping[str, object],
    physical: Mapping[str, str],
    environment: Mapping[str, str],
) -> list[str]:
    configured = bindings.get("aliases")
    values = configured.get("platformio") if isinstance(configured, Mapping) else None
    if not isinstance(values, Mapping):
        return []
    key = _target_key(physical, environment)
    identity = _physical_key(physical)
    return sorted(
        alias
        for alias, target in values.items()
        if isinstance(alias, str) and target in {key, identity}
    )


def _devices(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        _fail("inventory-malformed", "PlatformIO serial inventory is malformed")
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        port = item.get("port")
        if isinstance(port, str) and port and "\x00" not in port:
            result.append(dict(item))
    return tuple(result)


def _inventory(
    request: Mapping[str, object],
    executable: str,
    project: Path,
) -> tuple[tuple[dict[str, str], ...], tuple[dict[str, object], ...]]:
    config = _environment_metadata(
        _json(
            _run((executable, "project", "config", "--json-output"), cwd=project),
            label="project",
        )
    )
    environments = _requested_environments(request, config)
    devices = _devices(
        _json(
            _run((executable, "device", "list", "--json-output"), cwd=project),
            label="serial",
        )
    )
    return environments, devices


def _candidate(
    *,
    bindings: Mapping[str, object],
    device: Mapping[str, object],
    physical: Mapping[str, str],
    environment: Mapping[str, str],
) -> dict[str, object]:
    port = device["port"]
    assert isinstance(port, str)
    target_key = _target_key(physical, environment)
    return {
        "target_key": target_key,
        "label": f"PlatformIO {environment['board']} ({environment['name']})",
        "tags": ["embedded", "platformio"],
        "roles": ["embedded"],
        "aliases": _aliases(bindings, physical, environment),
        "capabilities": ["run", "logs", "upload"],
        "ready": True,
        "reason": None,
        "remediation": None,
        "preparation": [],
        "rank": [1],
        "binding": {
            "target_key": target_key,
            "platform": "platformio",
            "physical": dict(physical),
            "port": port,
            "environment": dict(environment),
        },
    }


def _identity_unknown() -> dict[str, object]:
    return {
        "target_key": "platformio-identity-unknown",
        "label": "PlatformIO serial device",
        "tags": ["embedded", "platformio"],
        "roles": ["embedded"],
        "aliases": [],
        "capabilities": [],
        "ready": False,
        "reason": "identity-unknown",
        "remediation": "Connect a board reporting stable USB VID, PID, and serial metadata; discovery will not select a pathname",
        "preparation": [],
        "rank": [0],
        "binding": {"platform": "platformio", "identity_status": "unknown"},
    }


def discover() -> None:
    request, bindings = load_request(), load_bindings()
    try:
        executable, project, _state_directory = _configured(bindings)
        environments, devices = _inventory(request, executable, project)
    except BackendError as error:
        emit_inventory(
            request,
            (),
            ("availability",),
            (
                {
                    "code": error.code,
                    "message": str(error),
                    "remediation": "Install or configure PlatformIO and a project without opening a serial port or provisioning a board",
                },
            ),
        )
        return
    physicals = [_physical(device) for device in devices]
    identity_counts: dict[str, int] = {}
    for physical in physicals:
        if physical is not None:
            identity = _physical_key(physical)
            identity_counts[identity] = identity_counts.get(identity, 0) + 1
    duplicate_identities = {
        identity for identity, count in identity_counts.items() if count > 1
    }
    candidates: list[dict[str, object]] = []
    identity_unknown = False
    for device, physical in zip(devices, physicals, strict=True):
        if physical is None or _physical_key(physical) in duplicate_identities:
            identity_unknown = True
            continue
        candidates.extend(
            _candidate(
                bindings=bindings,
                device=device,
                physical=physical,
                environment=environment,
            )
            for environment in environments
        )
    if identity_unknown:
        candidates.append(_identity_unknown())
    emit_inventory(request, candidates, ("availability",))


def _context_binding() -> dict[str, object]:
    private = load_context().get("private_binding")
    if not isinstance(private, Mapping) or set(private) != {
        "target_key",
        "platform",
        "physical",
        "port",
        "environment",
    }:
        _fail("identity-lost", "Selected PlatformIO target context is unavailable")
    physical, environment, port = (
        private["physical"],
        private["environment"],
        private["port"],
    )
    if (
        private["platform"] != "platformio"
        or not isinstance(physical, Mapping)
        or not isinstance(environment, Mapping)
        or not isinstance(port, str)
    ):
        _fail("identity-lost", "Selected PlatformIO target context is unavailable")
    normalized = _physical(physical)
    if (
        normalized is None
        or set(environment) != {"name", "platform", "board"}
        or any(not isinstance(value, str) for value in environment.values())
    ):
        _fail("identity-lost", "Selected PlatformIO target context is unavailable")
    sealed = {
        "physical": normalized,
        "environment": dict(environment),
        "port": port,
        "target_key": private["target_key"],
    }
    if not isinstance(sealed["target_key"], str) or sealed["target_key"] != _target_key(
        normalized, sealed["environment"]
    ):
        _fail("identity-lost", "Selected PlatformIO target identity changed")
    return sealed


def _selected(
    request: Mapping[str, object],
    bindings: Mapping[str, object],
) -> tuple[str, Path, Path, dict[str, object]]:
    executable, project, state_directory = _configured(bindings)
    sealed = _context_binding()
    config = _environment_metadata(
        _json(
            _run((executable, "project", "config", "--json-output"), cwd=project),
            label="project",
        )
    )
    selected_environment = sealed["environment"]
    assert isinstance(selected_environment, dict)
    if config.get(selected_environment["name"]) != selected_environment:
        _fail("identity-lost", "Selected PlatformIO environment changed")
    if selected_environment not in _requested_environments(request, config):
        _fail(
            "identity-lost",
            "Selected PlatformIO environment no longer matches this profile",
        )
    devices = _devices(
        _json(
            _run((executable, "device", "list", "--json-output"), cwd=project),
            label="serial",
        )
    )
    physical = sealed["physical"]
    assert isinstance(physical, dict)
    matches = [device for device in devices if _physical(device) == physical]
    if len(matches) != 1:
        _fail(
            "identity-lost",
            "Selected PlatformIO board identity is unavailable or ambiguous",
        )
    port = matches[0]["port"]
    assert isinstance(port, str)
    return executable, project, state_directory, {**sealed, "port": port}


def _monitor_paths(
    state_directory: Path,
    run_id: object,
    physical: Mapping[str, str],
    target_key: str,
    *,
    create: bool,
) -> tuple[Path, Path, Path]:
    if not isinstance(run_id, str) or not run_id:
        _fail("invalid", "PlatformIO monitor requires a selected run identity")
    run_directory = state_directory / hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    if create:
        try:
            run_directory.mkdir(mode=0o700, exist_ok=True)
            info = run_directory.stat()
        except OSError as error:
            raise BackendError(
                "runtime-unavailable", "PlatformIO monitor run state is unavailable"
            ) from error
        if (
            not run_directory.is_dir()
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            _fail("runtime-unavailable", "PlatformIO monitor run state is not private")
    physical_digest = hashlib.sha256(
        _physical_key(physical).encode("utf-8")
    ).hexdigest()
    target_digest = hashlib.sha256(target_key.encode("utf-8")).hexdigest()
    return (
        state_directory / f"{physical_digest}.lock",
        run_directory / f"{target_digest}.owner.json",
        run_directory / f"{target_digest}.log",
    )


def _lock(path: Path, *, blocking: bool, create: bool) -> int:
    flags = os.O_RDWR | os.O_CLOEXEC | (os.O_CREAT if create else 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _monitor_live(lock_path: Path) -> bool:
    try:
        descriptor = _lock(lock_path, blocking=False, create=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            return True
        raise BackendError(
            "runtime-unavailable", "PlatformIO monitor owner cannot be inspected"
        ) from error
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        return False


def _write_owner(
    path: Path,
    binding: Mapping[str, object],
    run_id: object,
    token: str,
) -> None:
    if not isinstance(run_id, str) or not run_id:
        _fail("invalid", "PlatformIO monitor requires a selected run identity")
    temporary = path.with_suffix(".tmp")
    payload = json.dumps(
        {"run_id": run_id, "binding": binding, "token": token},
        separators=(",", ":"),
        sort_keys=True,
    )
    with open(temporary, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _owner(path: Path, binding: Mapping[str, object], run_id: object) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BackendError(
            "owner-unavailable",
            "Selected PlatformIO monitor has no readable owner record",
        ) from error
    stored = value.get("binding") if isinstance(value, Mapping) else None
    if (
        not isinstance(stored, Mapping)
        or value.get("run_id") != run_id
        or any(
            stored.get(name) != binding.get(name)
            for name in ("target_key", "physical", "environment")
        )
    ):
        _fail(
            "identity-lost", "Selected PlatformIO monitor owner does not match this run"
        )


def _owned_identity(path: Path, token: str | None = None) -> tuple[int, int] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_size > _LIMIT
        ):
            return None
        if token is not None:
            value = json.loads(os.read(descriptor, _LIMIT + 1).decode("utf-8"))
            if not isinstance(value, Mapping) or value.get("token") != token:
                return None
        return info.st_dev, info.st_ino
    except OSError, UnicodeError, json.JSONDecodeError:
        return None
    finally:
        os.close(descriptor)


def _path_absent(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _unlink_owned(path: Path, identity: tuple[int, int] | None) -> bool:
    if identity is None:
        return _path_absent(path)
    try:
        info = os.stat(path, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != identity or not stat.S_ISREG(info.st_mode):
            return False
        path.unlink()
    except OSError:
        return False
    return _path_absent(path)


def _cleanup_owned_state(
    owner_path: Path,
    log_path: Path,
    token: str,
    log_identity: tuple[int, int] | None,
) -> bool:
    owner_removed = _unlink_owned(owner_path, _owned_identity(owner_path, token))
    log_removed = _unlink_owned(log_path, log_identity)
    try:
        owner_path.parent.rmdir()
    except OSError:
        pass
    return owner_removed and log_removed


def _monitor_argv(executable: str, binding: Mapping[str, object]) -> tuple[str, ...]:
    environment, port = binding["environment"], binding["port"]
    assert isinstance(environment, Mapping) and isinstance(port, str)
    return (
        executable,
        "device",
        "monitor",
        "--environment",
        str(environment["name"]),
        "--port",
        port,
        "--no-reconnect",
    )


def _upload_argv(executable: str, binding: Mapping[str, object]) -> tuple[str, ...]:
    environment, port = binding["environment"], binding["port"]
    assert isinstance(environment, Mapping) and isinstance(port, str)
    return (
        executable,
        "run",
        "--environment",
        str(environment["name"]),
        "--target",
        "upload",
        "--upload-port",
        port,
    )


def _monitor_admitted(output: bytes, port: str) -> bool:
    """Accept PlatformIO's selected-port monitor banner."""
    text = output.lower()
    return b"--- terminal on " in text and port.encode("utf-8").lower() in text


def _record_monitor_output(log: BinaryIO, data: bytes) -> None:
    log.write(data)
    log.seek(0, os.SEEK_END)
    if log.tell() > _RETAINED_LOG_BYTES * 2:
        log.seek(-_RETAINED_LOG_BYTES, os.SEEK_END)
        tail = log.read(_RETAINED_LOG_BYTES)
        log.seek(0)
        log.truncate()
        log.write(tail)
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _read_pty(descriptor: int) -> bytes:
    try:
        return os.read(descriptor, 8192)
    except OSError as error:
        if error.errno == errno.EIO:
            return b""
        raise


def _physical_binding(binding: Mapping[str, object]) -> dict[str, str]:
    physical = _physical(binding.get("physical"))
    if physical is None:
        _fail("identity-lost", "Selected PlatformIO board identity changed")
    return physical


def _monitor(
    executable: str,
    project: Path,
    state_directory: Path,
    binding: Mapping[str, object],
    context: Mapping[str, object],
) -> None:
    owner = OwnerContext.from_environ()
    owner.begin()
    target_key = binding["target_key"]
    assert isinstance(target_key, str)
    try:
        lock_path, owner_path, log_path = _monitor_paths(
            state_directory,
            context.get("run_id"),
            _physical_binding(binding),
            target_key,
            create=True,
        )
    except Exception:
        owner.finish(cleanup_known=True)
        raise
    child_reaped = False
    child_started = False
    terminal_closed = False
    try:
        lock = _lock(lock_path, blocking=False, create=True)
    except OSError as error:
        owner.finish(cleanup_known=True)
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            _fail("busy", "Selected PlatformIO board already has a monitor owner")
        raise BackendError(
            "runtime-unavailable", "PlatformIO monitor lock is unavailable"
        ) from error
    owner_token = secrets.token_hex(32)
    log_identity: tuple[int, int] | None = None
    try:
        _write_owner(owner_path, binding, context.get("run_id"), owner_token)
        with open(log_path, "a+b", buffering=0) as log:
            info = os.fstat(log.fileno())
            log_identity = (info.st_dev, info.st_ino)
            master = slave = -1
            try:
                master, slave = pty.openpty()
                process = subprocess.Popen(
                    _monitor_argv(executable, binding),
                    cwd=project,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env={
                        key: value
                        for key, value in os.environ.items()
                        if not key.startswith("MSHIP_")
                    },
                )
                child_started = True
            except OSError as error:
                if master >= 0:
                    os.close(master)
                raise BackendError(
                    "tool-unavailable", "Configured PlatformIO monitor is unavailable"
                ) from error
            finally:
                if slave >= 0:
                    os.close(slave)
            admitted = bytearray()
            acknowledged = False
            stop = False
            previous_handlers: dict[int, object] = {}
            port = binding["port"]
            assert isinstance(port, str)

            def request_stop(_signum: int, _frame: object) -> None:
                nonlocal stop
                stop = True

            try:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous_handlers[signum] = signal.signal(signum, request_stop)
                while not stop and process.poll() is None:
                    ready, _, _ = select.select((master,), (), (), 0.25)
                    if ready:
                        data = _read_pty(master)
                        if data:
                            _record_monitor_output(log, data)
                            if not acknowledged:
                                admitted.extend(data)
                                del admitted[:-4096]
                                if (
                                    _monitor_admitted(admitted, port)
                                    and process.poll() is None
                                ):
                                    owner.ready()
                                    acknowledged = True
                if not stop:
                    while data := _read_pty(master):
                        _record_monitor_output(log, data)
                    if process.returncode:
                        _fail(
                            "monitor-failed",
                            "Selected PlatformIO monitor exited unsuccessfully",
                        )
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                child_reaped = process.poll() is not None
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
                os.close(master)
                terminal_closed = True
    finally:
        cleanup_verified = _cleanup_owned_state(
            owner_path,
            log_path,
            owner_token,
            log_identity,
        )
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        owner.finish(
            cleanup_known=cleanup_verified
            and (not child_started or child_reaped and terminal_closed)
        )


def run(request: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    executable, project, state_directory, binding = _selected(request, bindings)
    context = load_context()
    _monitor(executable, project, state_directory, binding, context)


def logs(request: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    _executable, _project, state_directory, binding = _selected(request, bindings)
    target_key = binding["target_key"]
    assert isinstance(target_key, str)
    context = load_context()
    lock_path, owner_path, log_path = _monitor_paths(
        state_directory,
        context.get("run_id"),
        _physical_binding(binding),
        target_key,
        create=False,
    )
    if not _monitor_live(lock_path):
        _fail("owner-unavailable", "Selected PlatformIO monitor is not running")
    _owner(owner_path, binding, context.get("run_id"))
    try:
        with open(log_path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            while _monitor_live(lock_path):
                offset = stream.tell()
                end = stream.seek(0, os.SEEK_END)
                if offset > end:
                    # The owner compacted its bounded tail. Resume from that tail
                    # rather than waiting for the new file to regrow past our old
                    # offset and silently losing current monitor output.
                    stream.seek(0)
                else:
                    stream.seek(offset)
                data = stream.read(8192)
                if data:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                else:
                    time.sleep(0.25)
    except OSError as error:
        raise BackendError(
            "owner-unavailable", "Selected PlatformIO monitor logs are unavailable"
        ) from error


def upload(request: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    executable, project, state_directory, binding = _selected(request, bindings)
    target_key = binding["target_key"]
    assert isinstance(target_key, str)
    context = load_context()
    lock_path, _owner_path, _log_path = _monitor_paths(
        state_directory,
        context.get("run_id"),
        _physical_binding(binding),
        target_key,
        create=False,
    )
    try:
        lock = _lock(lock_path, blocking=False, create=True)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            _fail("busy", "Stop the selected PlatformIO monitor before uploading")
        raise BackendError(
            "runtime-unavailable", "PlatformIO upload lock is unavailable"
        ) from error
    try:
        result = subprocess.run(
            _upload_argv(executable, binding),
            cwd=project,
            stdin=subprocess.DEVNULL,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("MSHIP_")
            },
            check=False,
        )
    except OSError as error:
        raise BackendError(
            "tool-unavailable", "Configured PlatformIO upload is unavailable"
        ) from error
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
    if result.returncode:
        _fail("upload-failed", "Selected PlatformIO upload failed")


def main(argv: Sequence[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] != "discover"):
        print("usage: backend.py [discover]", file=sys.stderr)
        return 2
    try:
        request = load_request()
        if len(argv) == 2:
            discover()
        elif request.get("operation") == "run":
            run(request, load_bindings())
        elif request.get("operation") == "logs":
            logs(request, load_bindings())
        elif request.get("operation") == "upload":
            upload(request, load_bindings())
        else:
            _fail("invalid", "Requested PlatformIO operation is unavailable")
    except (BackendError, ExampleError, SessionError) as error:
        print(
            f"PlatformIO backend {getattr(error, 'code', 'invalid')}: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
