"""Shared wire boundary for built-in run-target backends."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence

from mship.core.run_target.models import DiscoveryRequest, DiscoveryResult

_LIMIT = 1024 * 1024


class ExampleError(ValueError):
    """Safe backend configuration or protocol failure."""


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExampleError("duplicate backend input field")
        result[key] = value
    return result


def _load(name: str) -> dict:
    path = os.environ.get(name)
    if not path:
        raise ExampleError("backend requires owner-provided private inputs")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.geteuid()
                or info.st_size > _LIMIT
            ):
                raise ExampleError("backend input is not a bounded private file")
            data = stream.read(_LIMIT + 1)
        if len(data) > _LIMIT:
            raise ExampleError("backend input exceeds its size limit")
        value = json.loads(data, object_pairs_hook=_object)
        if not isinstance(value, dict):
            raise ExampleError("backend input must be an object")
        return value
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise ExampleError("invalid owner-provided backend input") from error


def load_request() -> dict:
    try:
        return DiscoveryRequest.model_validate(
            _load("MSHIP_TARGET_REQUEST_FILE")
        ).model_dump(mode="json")
    except ValueError as error:
        raise ExampleError("invalid backend discovery request") from error


def load_bindings() -> dict:
    value = _load("MSHIP_TARGET_BINDINGS_FILE")
    if set(value) != {"paths", "aliases"} or not all(
        isinstance(value[key], dict) for key in value
    ):
        raise ExampleError("invalid backend host bindings")
    return value


def load_context() -> dict:
    value = _load("MSHIP_TARGET_CONTEXT_FILE")
    request = load_request()
    if (
        any(
            value.get(key) != request[key]
            for key in (
                "protocol_version",
                "task",
                "repo",
                "profile",
                "profile_revision",
                "backend",
                "backend_revision",
            )
        )
        or not isinstance(value.get("private_binding"), dict)
        or not value.get("run_id")
    ):
        raise ExampleError("selected backend context does not match this request")
    return value


def emit_inventory(
    request: Mapping,
    candidates: Sequence[Mapping],
    rank_schema: Sequence[str] = (),
    errors: Sequence[Mapping] = (),
) -> None:
    try:
        result = DiscoveryResult.model_validate(
            {
                "protocol_version": 1,
                "backend": request["backend"],
                "backend_revision": request["backend_revision"],
                "rank_schema": list(rank_schema),
                "candidates": list(candidates),
                "errors": list(errors),
            }
        )
        data = result.model_dump_json()
        if len(data.encode()) > _LIMIT:
            raise ExampleError("backend inventory exceeds its size limit")
    except (ValueError, KeyError) as error:
        raise ExampleError("invalid backend inventory") from error
    print(data, flush=True)
