"""Bounded discovery invocation and protocol parsing for run-target backends."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Callable, Mapping

import yaml
from pydantic import ValidationError

from mship.core.run_host.config import HostRegistration
from mship.core.run_target.models import (
    BackendConfig,
    BackendExecution,
    BackendResult,
    DiscoveryRequest,
    DiscoveryResult,
    HostInventory,
    JsonValue,
    TargetSelectionError,
    _json_value,
)

DISCOVERY_STDOUT_LIMIT = 1024 * 1024
DISCOVERY_STDERR_LIMIT = 256 * 1024
DISCOVERY_TIMEOUT_SECONDS = 60
HOST_BINDINGS_MAX_BYTES = 1024 * 1024

BackendExecutor = Callable[[HostRegistration, BackendExecution], BackendResult]


def _protocol_error(detail: str = "invalid discovery result") -> TargetSelectionError:
    return TargetSelectionError(
        "backend_protocol", "backend emitted invalid discovery data", (detail,)
    )


def parse_discovery_result(payload: bytes, *, max_bytes: int) -> DiscoveryResult:
    """Parse the one-object, host-free discovery protocol without leaking output."""
    if len(payload) > max_bytes:
        raise _protocol_error("stdout_limit")
    try:
        decoded = payload.decode("utf-8")
        raw = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _protocol_error("json") from error
    if not isinstance(raw, dict):
        raise _protocol_error("object")
    expected = {
        "protocol_version",
        "backend",
        "backend_revision",
        "rank_schema",
        "candidates",
        "errors",
    }
    if set(raw) != expected:
        raise _protocol_error("control_fields")
    try:
        result = DiscoveryResult.model_validate(raw)
    except ValidationError as error:
        raise _protocol_error("schema") from error
    target_keys = [candidate.target_key for candidate in result.candidates]
    if len(target_keys) != len(set(target_keys)):
        raise _protocol_error("duplicate_target")
    if any(
        len(candidate.rank) != len(result.rank_schema)
        for candidate in result.candidates
    ):
        raise _protocol_error("rank_arity")
    return result


def host_bindings_path(home: Path, environ: Mapping[str, str] = os.environ) -> Path:
    """Return the host-local private bindings path using the shared XDG policy."""
    value = environ.get("XDG_CONFIG_HOME", "")
    base = Path(value) if value and Path(value).is_absolute() else home / ".config"
    return base / "mothership" / "run-target-bindings.yaml"


def _read_private_bindings(path: Path) -> bytes | None:
    """Read one owner-private regular file without following a replacement link."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _protocol_error("bindings") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > HOST_BINDINGS_MAX_BYTES
        ):
            raise _protocol_error("bindings")
        chunks: list[bytes] = []
        remaining = HOST_BINDINGS_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > HOST_BINDINGS_MAX_BYTES:
            raise _protocol_error("bindings")
        return payload
    except OSError as error:
        raise _protocol_error("bindings") from error
    finally:
        os.close(descriptor)


def load_host_bindings(path: Path, backend: str) -> dict[str, JsonValue]:
    """Load only one backend's bounded private path and alias data."""
    if not backend:
        raise _protocol_error("backend")
    payload = _read_private_bindings(path)
    if payload is None:
        return {"paths": {}, "aliases": {}}
    try:
        raw = yaml.safe_load(payload)
    except (UnicodeError, yaml.YAMLError) as error:
        raise _protocol_error("bindings") from error
    if (
        not isinstance(raw, dict)
        or set(raw) != {"version", "backends"}
        or raw.get("version") != 1
    ):
        raise _protocol_error("bindings")
    backends = raw["backends"]
    if not isinstance(backends, dict):
        raise _protocol_error("bindings")
    entry = backends.get(backend, {"paths": {}, "aliases": {}})
    if not isinstance(entry, dict) or set(entry) != {"paths", "aliases"}:
        raise _protocol_error("bindings")
    try:
        paths = _json_value(entry["paths"])
        aliases = _json_value(entry["aliases"])
    except ValueError as error:
        raise _protocol_error("bindings") from error
    if not isinstance(paths, dict) or not isinstance(aliases, dict):
        raise _protocol_error("bindings")
    return {"paths": paths, "aliases": aliases}


def _failure(
    host: HostRegistration, request: DiscoveryRequest, code: str
) -> HostInventory:
    return HostInventory(
        host=host,
        backend_revision=request.backend_revision,
        rank_schema=(),
        operation=request.operation,
        profile_revision=request.profile_revision,
        candidates=(),
        error=code,
    )


def discover_on_host(
    host: HostRegistration,
    request: DiscoveryRequest,
    config: BackendConfig,
    *,
    execute: BackendExecutor,
) -> HostInventory:
    """Run bounded read-only discovery through the injected supervised executor."""
    if request.operation not in config.operations:
        raise TargetSelectionError(
            "constraint_conflict", "requested operation is not supported by backend"
        )
    execution = BackendExecution(
        task=request.task,
        repo=request.repo,
        profile=request.profile,
        backend=request.backend,
        logical_task=config.discover_task,
        operation=request.operation,
        request=request.model_dump(mode="json"),
        run_id=None,
        preparation="discover",
    )
    result = execute(host, execution)
    if result.error_code is not None:
        return _failure(
            host,
            request,
            "backend_timeout"
            if result.error_code == "timeout"
            else "backend_transport",
        )
    if result.exit_code is None:
        return _failure(host, request, "backend_transport")
    if result.exit_code != 0:
        return _failure(host, request, "backend_nonzero")
    if (
        len(result.stdout) > DISCOVERY_STDOUT_LIMIT
        or len(result.stderr) > DISCOVERY_STDERR_LIMIT
    ):
        return _failure(host, request, "backend_protocol")
    try:
        parsed = parse_discovery_result(result.stdout, max_bytes=DISCOVERY_STDOUT_LIMIT)
    except TargetSelectionError:
        return _failure(host, request, "backend_protocol")
    if (
        parsed.backend != request.backend
        or parsed.backend_revision != request.backend_revision
    ):
        return _failure(host, request, "backend_protocol")
    candidates = parsed.candidates
    if request.target_alias is not None:
        candidates = tuple(
            candidate
            for candidate in candidates
            if request.target_alias in candidate.aliases
        )
    return HostInventory(
        host=host,
        backend_revision=parsed.backend_revision,
        rank_schema=parsed.rank_schema,
        operation=request.operation,
        profile_revision=request.profile_revision,
        candidates=candidates,
        error="backend_reported_error" if parsed.errors else None,
    )
