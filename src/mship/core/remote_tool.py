"""Typed, bounded wire contract for task-aware remote tool operations.

The types in this module are deliberately independent of HTTP and process
ownership so both client and server share one strict contract.  Untrusted wire
input raises :class:`ToolProtocolError` with payload-free messages.
"""

from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Literal, Mapping

from mship.core.run_ref import is_run_ref_segment
from mship.core.session_inputs import InstallFromResult, SessionError

MAX_REQUEST_BYTES = 128 * 1024
MAX_EVENT_FRAME_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_CHUNK_BYTES = 16 * 1024
MAX_DISCOVERY_STDOUT_BYTES = 1024 * 1024
MAX_DISCOVERY_STDERR_BYTES = 256 * 1024
_MAX_NAME_BYTES = 128
_MAX_TEXT_BYTES = 4096
_MAX_ARGV_COUNT = 256
_MAX_ENV_COUNT = 128
_MAX_RUN_REF_REPOS = 128
_MAX_HEADER_BYTES = 256

_STATUS_VALUES = frozenset(
    {
        "running",
        "completed",
        "invalid",
        "busy",
        "auth_error",
        "materialization_error",
        "launch_error",
        "protocol_error",
        "stdout_limit",
        "stderr_limit",
        "timeout",
        "cancelled",
        "unknown",
        "evidence_error",
        "unsupported",
        "unreachable",
        "unauthed",
        "unauthorized",
        "workspace_unavailable",
    }
)
_PREPARATIONS = frozenset({"discover", "launch", "observe"})
_EVENT_KINDS = frozenset({"started", "ready", "stdout", "stderr", "result"})
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEX_REVISION = re.compile(r"^[0-9a-fA-F]{7,64}$")


class ToolProtocolError(ValueError):
    """A malformed tool request, result, event, or wire frame.

    Messages intentionally identify only the contract boundary, never raw
    request argv, environment values, bearer credentials, or child output.
    """


def _reject(message: str) -> None:
    raise ToolProtocolError(message)


def _text(
    value: object,
    *,
    field_name: str,
    limit: int = _MAX_TEXT_BYTES,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        _reject(f"invalid {field_name}")
    if not allow_empty and not value:
        _reject(f"invalid {field_name}")
    if "\x00" in value or any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        _reject(f"invalid {field_name}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _reject(f"invalid {field_name}")
    if len(encoded) > limit:
        _reject(f"invalid {field_name}")
    return value


def _name(value: object, *, field_name: str) -> str:
    value = _text(value, field_name=field_name, limit=_MAX_NAME_BYTES)
    if not is_run_ref_segment(value):
        _reject(f"invalid {field_name}")
    return value


def _optional_text(
    value: object, *, field_name: str, limit: int = _MAX_TEXT_BYTES
) -> str | None:
    if value is None:
        return None
    return _text(value, field_name=field_name, limit=limit)


def _optional_revision(value: object) -> str | None:
    if value is None:
        return None
    value = _text(value, field_name="source revision", limit=64)
    if not _HEX_REVISION.fullmatch(value):
        _reject("invalid source revision")
    return value


def _optional_result_id(value: object) -> str | None:
    if value is None:
        return None
    value = _text(value, field_name="task result id", limit=128)
    if re.fullmatch(r"[A-Za-z0-9_-]{24,128}", value) is None:
        _reject("invalid task result id")
    return value

def _positive_finite(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(f"invalid {field_name}")
    try:
        value = float(value)
    except OverflowError:
        _reject(f"invalid {field_name}")
    if not math.isfinite(value) or value <= 0:
        _reject(f"invalid {field_name}")
    return value


def _optional_timeout(value: object) -> float | None:
    if value is None:
        return None
    return _positive_finite(value, field_name="timeout")


def _strict_b64(value: object, *, field_name: str, max_bytes: int) -> bytes:
    if not isinstance(value, str):
        _reject(f"invalid {field_name}")
    # Four base64 characters can decode to at most three bytes. Refuse before
    # decoding so hostile JSON cannot cause a large allocation.
    if len(value) > ((max_bytes + 2) // 3) * 4:
        _reject(f"invalid {field_name}")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except UnicodeEncodeError, ValueError:
        _reject(f"invalid {field_name}")
    if len(decoded) > max_bytes:
        _reject(f"invalid {field_name}")
    return decoded


def _object(data: object, *, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        _reject("invalid tool payload")
    if set(data) != fields:
        _reject("invalid tool payload")
    return data


def _owner_pair(
    owner_ref: object, generation: object, *, preparation: str | None = None
) -> tuple[str | None, str | None]:
    owner = _optional_text(
        owner_ref, field_name="owner reference", limit=_MAX_NAME_BYTES
    )
    generation_value = _optional_text(
        generation, field_name="generation", limit=_MAX_NAME_BYTES
    )
    if (owner is None) != (generation_value is None):
        _reject("invalid owner reference")
    if preparation is not None:
        if preparation == "observe" and owner is None:
            _reject("missing owner reference")
        if preparation != "observe" and owner is not None:
            _reject("owner reference is only valid for observation")
    return owner, generation_value


@dataclass(frozen=True)
class ToolRequest:
    task: str
    repo: str
    argv: tuple[str, ...]
    task_key: str | None = None
    input_files: Mapping[str, str] = field(default_factory=dict, repr=False)
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    cwd: str = "."
    preparation: Literal["discover", "launch", "observe"] = "launch"
    run_ref_repos: tuple[str, ...] = ()
    source_revision: str | None = None
    owner_ref: str | None = None
    generation: str | None = None
    max_stdout_bytes: int | None = None
    max_stderr_bytes: int | None = None
    timeout_seconds: float | None = None
    # Server-recognized host-tools operation; no caller argv may accompany it.
    host_tools_action: Literal["diagnose", "bootstrap"] | None = None
    install_from_result: InstallFromResult | None = None

    def __post_init__(self) -> None:
        task = _name(self.task, field_name="task")
        repo = _name(self.repo, field_name="repo")
        task_key = (
            None
            if self.task_key is None
            else _name(self.task_key, field_name="task key")
        )
        if not isinstance(self.argv, tuple) or len(self.argv) > _MAX_ARGV_COUNT:
            _reject("invalid argv")
        argv = tuple(_text(value, field_name="argv") for value in self.argv)
        if not argv and self.preparation != "observe" and task_key is None and self.host_tools_action is None:
            _reject("argv is required")
        if task_key is not None and argv:
            _reject("task key requires empty argv")
        if self.host_tools_action is not None:
            if self.host_tools_action not in {"diagnose", "bootstrap"} or argv or task_key is not None:
                _reject("invalid host tools action")
        if self.install_from_result is not None and (
            not isinstance(self.install_from_result, InstallFromResult)
            or task_key is None
            or self.preparation == "discover"
            or self.host_tools_action is not None
        ):
            _reject("invalid session installation")
        if (
            not isinstance(self.input_files, Mapping)
            or len(self.input_files) > _MAX_ENV_COUNT
        ):
            _reject("invalid input files")
        input_files: dict[str, str] = {}
        for key, value in self.input_files.items():
            if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
                _reject("invalid input files")
            if key in {"MSHIP_TASK", "MSHIP_REPO", "MSHIP_SOURCE_REVISION"}:
                _reject("invalid input files")
            if not isinstance(value, str):
                _reject("invalid input file content")
            try:
                if len(value.encode("utf-8")) > MAX_REQUEST_BYTES:
                    _reject("invalid input file content")
            except UnicodeError:
                _reject("invalid input file content")
            input_files[key] = value
        if not isinstance(self.env, Mapping) or len(self.env) > _MAX_ENV_COUNT:
            _reject("invalid environment")
        env: dict[str, str] = {}
        for key, value in self.env.items():
            if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
                _reject("invalid environment")
            env[key] = _text(value, field_name="environment value", allow_empty=True)
        cwd = _text(self.cwd, field_name="cwd", limit=_MAX_TEXT_BYTES)
        if (
            cwd.startswith("/")
            or "\\" in cwd
            or any(part in {"", ".", ".."} for part in cwd.split("/"))
            and cwd != "."
        ):
            _reject("invalid cwd")
        run_ref_repos = self.run_ref_repos
        if (
            not isinstance(run_ref_repos, tuple)
            or len(run_ref_repos) > _MAX_RUN_REF_REPOS
        ):
            _reject("invalid run references")
        run_ref_repos = tuple(
            _name(value, field_name="run reference") for value in run_ref_repos
        )
        if len(set(run_ref_repos)) != len(run_ref_repos):
            _reject("invalid run references")
        source_revision = _optional_revision(self.source_revision)
        owner_ref, generation = _owner_pair(
            self.owner_ref,
            self.generation,
            preparation=self.preparation,
        )
        stdout_cap = self.max_stdout_bytes
        stderr_cap = self.max_stderr_bytes
        if self.preparation == "discover":
            if (
                isinstance(stdout_cap, bool)
                or not isinstance(stdout_cap, int)
                or not 0 < stdout_cap <= MAX_DISCOVERY_STDOUT_BYTES
            ):
                _reject("invalid stdout limit")
            if (
                isinstance(stderr_cap, bool)
                or not isinstance(stderr_cap, int)
                or not 0 < stderr_cap <= MAX_DISCOVERY_STDERR_BYTES
            ):
                _reject("invalid stderr limit")
        elif stdout_cap is not None or stderr_cap is not None:
            _reject("collection limits are only valid for discovery")
        timeout = _optional_timeout(self.timeout_seconds)
        if self.preparation == "discover" and timeout is None:
            _reject("discovery requires a timeout")
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "repo", repo)
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "task_key", task_key)
        object.__setattr__(self, "input_files", MappingProxyType(dict(input_files)))
        object.__setattr__(self, "env", MappingProxyType(dict(env)))
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "run_ref_repos", run_ref_repos)
        object.__setattr__(self, "source_revision", source_revision)
        object.__setattr__(self, "owner_ref", owner_ref)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "host_tools_action", self.host_tools_action)
        object.__setattr__(self, "timeout_seconds", timeout)
        try:
            request_size = len(
                json.dumps(
                    self.to_dict(), separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
            )
        except TypeError, UnicodeError:
            _reject("invalid tool request")
        if request_size > MAX_REQUEST_BYTES:
            _reject("tool request exceeds size limit")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "task": self.task,
            "repo": self.repo,
            "argv": list(self.argv),
            "task_key": self.task_key,
            "input_files": dict(self.input_files),
            "env": dict(self.env),
            "cwd": self.cwd,
            "preparation": self.preparation,
            "run_ref_repos": list(self.run_ref_repos),
            "source_revision": self.source_revision,
            "owner_ref": self.owner_ref,
            "generation": self.generation,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.host_tools_action is not None:
            payload["host_tools_action"] = self.host_tools_action
        if self.install_from_result is not None:
            payload["install_from_result"] = self.install_from_result.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: object) -> "ToolRequest":
        if not isinstance(data, Mapping):
            _reject("invalid tool payload")
        fields = frozenset(
            {
                "task",
                "repo",
                "argv",
                "task_key",
                "input_files",
                "env",
                "cwd",
                "preparation",
                "run_ref_repos",
                "source_revision",
                "owner_ref",
                "generation",
                "max_stdout_bytes",
                "max_stderr_bytes",
                "timeout_seconds",
                "host_tools_action",
                "install_from_result",
            }
        )
        optional = frozenset({"task_key", "input_files", "host_tools_action", "install_from_result"})
        if set(data) - fields or not (fields - optional) <= set(data):
            _reject("invalid tool payload")
        if not isinstance(data["argv"], list) or not isinstance(
            data["run_ref_repos"], list
        ):
            _reject("invalid tool payload")
        try:
            install = (
                None if data.get("install_from_result") is None
                else InstallFromResult.from_dict(data["install_from_result"])
            )
        except SessionError:
            _reject("invalid session installation")
        return cls(
            task=data["task"],
            repo=data["repo"],
            argv=tuple(data["argv"]),
            task_key=data.get("task_key"),
            input_files=data.get("input_files", {}),
            env=data["env"],
            cwd=data["cwd"],
            preparation=data["preparation"],
            run_ref_repos=tuple(data["run_ref_repos"]),
            source_revision=data["source_revision"],
            owner_ref=data["owner_ref"],
            generation=data["generation"],
            max_stdout_bytes=data["max_stdout_bytes"],
            max_stderr_bytes=data["max_stderr_bytes"],
            timeout_seconds=data["timeout_seconds"],
            host_tools_action=data.get("host_tools_action"),
            install_from_result=install,
        )


@dataclass(frozen=True)
class ToolResult:
    status: str
    exit_code: int | None = None
    owner_ref: str | None = None
    generation: str | None = None
    source_revision: str | None = None
    result_id: str | None = None
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)
    # Optional server-created safe host-tools projection. Never reflects request
    # argv/env or child output and remains omitted for ordinary tool results.
    host_tools_report: Mapping[str, object] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in _STATUS_VALUES:
            _reject("invalid result status")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            _reject("invalid exit code")
        if self.status == "completed" and self.exit_code is None:
            _reject("completed result is missing exit code")
        if self.status != "completed" and self.exit_code is not None:
            _reject("infrastructure result has an exit code")
        owner_ref, generation = _owner_pair(self.owner_ref, self.generation)
        source_revision = _optional_revision(self.source_revision)
        if (
            not isinstance(self.stdout, bytes)
            or len(self.stdout) > MAX_DISCOVERY_STDOUT_BYTES
        ):
            _reject("invalid stdout")
        if (
            not isinstance(self.stderr, bytes)
            or len(self.stderr) > MAX_DISCOVERY_STDERR_BYTES
        ):
            _reject("invalid stderr")
        report = self.host_tools_report
        if report is not None:
            if not isinstance(report, Mapping):
                _reject("invalid host tools report")
            try:
                encoded_report = json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
            except (TypeError, UnicodeError):
                _reject("invalid host tools report")
            if len(encoded_report) > _MAX_TEXT_BYTES * 4:
                _reject("invalid host tools report")
            report = MappingProxyType(dict(report))
        object.__setattr__(self, "owner_ref", owner_ref)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "source_revision", source_revision)
        object.__setattr__(self, "host_tools_report", report)
        object.__setattr__(self, "result_id", _optional_result_id(self.result_id))

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "exit_code": self.exit_code,
            "owner_ref": self.owner_ref,
            "generation": self.generation,
            "source_revision": self.source_revision,
            "stdout": base64.b64encode(self.stdout).decode("ascii"),
            "stderr": base64.b64encode(self.stderr).decode("ascii"),
        }
        if self.host_tools_report is not None:
            payload["host_tools_report"] = dict(self.host_tools_report)
        if self.result_id is not None:
            payload["result_id"] = self.result_id
        return payload

    @classmethod
    def from_dict(cls, data: object) -> "ToolResult":
        if not isinstance(data, Mapping):
            _reject("invalid tool payload")
        fields = {
            "status", "exit_code", "owner_ref", "generation", "source_revision",
            "stdout", "stderr", "result_id", "host_tools_report",
        }
        if set(data) - fields or not (fields - {"result_id", "host_tools_report"}) <= set(data):
            _reject("invalid tool payload")
        return cls(
            status=data["status"],
            exit_code=data["exit_code"],
            owner_ref=data["owner_ref"],
            generation=data["generation"],
            source_revision=data["source_revision"],
            stdout=_strict_b64(
                data["stdout"],
                field_name="stdout",
                max_bytes=MAX_DISCOVERY_STDOUT_BYTES,
            ),
            stderr=_strict_b64(
                data["stderr"],
                field_name="stderr",
                max_bytes=MAX_DISCOVERY_STDERR_BYTES,
            ),
            host_tools_report=data.get("host_tools_report"),
            result_id=data.get("result_id"),
        )


@dataclass(frozen=True)
class ToolEvent:
    kind: Literal["started", "ready", "stdout", "stderr", "result"]
    data: bytes = field(default=b"", repr=False)
    result: ToolResult | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in _EVENT_KINDS:
            _reject("invalid event kind")
        if not isinstance(self.data, bytes) or len(self.data) > MAX_OUTPUT_CHUNK_BYTES:
            _reject("invalid event data")
        if self.kind in {"started", "ready"}:
            if (
                self.data
                or not isinstance(self.result, ToolResult)
                or self.result.status != "running"
            ):
                _reject("invalid owner event")
        elif self.kind in {"stdout", "stderr"}:
            if self.result is not None:
                _reject("invalid output event")
        elif self.data or not isinstance(self.result, ToolResult):
            _reject("invalid result event")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "data": base64.b64encode(self.data).decode("ascii"),
            "result": self.result.to_dict() if self.result is not None else None,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ToolEvent":
        data = _object(data, fields=frozenset({"kind", "data", "result"}))
        result = (
            None if data["result"] is None else ToolResult.from_dict(data["result"])
        )
        return cls(
            kind=data["kind"],
            data=_strict_b64(
                data["data"], field_name="event data", max_bytes=MAX_OUTPUT_CHUNK_BYTES
            ),
            result=result,
        )


@dataclass(frozen=True)
class ToolContext:
    task: str
    repo: str
    worktree: Path
    source_revision: str
    env_runner: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "task", _name(self.task, field_name="task"))
        object.__setattr__(self, "repo", _name(self.repo, field_name="repo"))
        if not isinstance(self.worktree, Path) or not self.worktree.is_absolute():
            _reject("invalid worktree")
        object.__setattr__(
            self, "source_revision", _optional_revision(self.source_revision) or ""
        )
        if not self.source_revision:
            _reject("invalid source revision")
        object.__setattr__(
            self,
            "env_runner",
            _optional_text(self.env_runner, field_name="environment runner"),
        )


def _nonce(value: object) -> str:
    value = _text(value, field_name="nonce", limit=_MAX_NAME_BYTES)
    if any(character.isspace() for character in value):
        _reject("invalid nonce")
    return value


def encode_tool_event(event: ToolEvent, nonce: str) -> bytes:
    """Encode one nonce-authenticated, length-prefixed event frame."""
    nonce = _nonce(nonce)
    if not isinstance(event, ToolEvent):
        _reject("invalid tool event")
    try:
        payload = json.dumps(
            event.to_dict(), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except TypeError, UnicodeError:
        _reject("invalid tool event")
    if len(payload) > MAX_EVENT_FRAME_BYTES:
        _reject("tool event exceeds frame limit")
    return f"__MSHIP_TOOL__:{nonce} {len(payload)}\n".encode("ascii") + payload


def iter_tool_events(chunks: Iterator[bytes], nonce: str) -> Iterator[ToolEvent]:
    """Incrementally decode a complete, nonce-authenticated event sequence.

    The sequence must contain exactly one terminal ``result`` event and no
    trailing frame.  Buffer growth is capped before every append.
    """
    nonce = _nonce(nonce)
    prefix = f"__MSHIP_TOOL__:{nonce} ".encode("ascii")
    buffer = bytearray()
    terminal = False
    saw_terminal = False
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            _reject("invalid tool stream")
        if len(chunk) > MAX_EVENT_FRAME_BYTES + _MAX_HEADER_BYTES:
            _reject("tool frame exceeds limit")
        if len(buffer) + len(chunk) > MAX_EVENT_FRAME_BYTES + _MAX_HEADER_BYTES:
            _reject("tool frame exceeds limit")
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                if len(buffer) > _MAX_HEADER_BYTES:
                    _reject("malformed tool frame")
                break
            header = bytes(buffer[:newline])
            if len(header) > _MAX_HEADER_BYTES:
                _reject("malformed tool frame")
            del buffer[: newline + 1]
            if terminal:
                _reject("duplicate tool result")
            if not header.startswith(prefix):
                _reject("wrong nonce or malformed tool frame")
            raw_length = header[len(prefix) :]
            if not raw_length or not raw_length.isascii() or not raw_length.isdigit():
                _reject("malformed tool frame")
            length = int(raw_length)
            if length > MAX_EVENT_FRAME_BYTES:
                _reject("tool frame exceeds limit")
            if len(buffer) < length:
                # Keep the parsed header with its payload while more chunks
                # arrive.  The header is revalidated only once, not treated as
                # child output.
                buffer[:0] = header + b"\n"
                break
            # Strip the header we restored above, then consume exactly payload.
            if buffer.startswith(header + b"\n"):
                del buffer[: len(header) + 1]
            payload = bytes(buffer[:length])
            del buffer[:length]
            try:
                decoded = json.loads(payload.decode("utf-8"))
            except UnicodeDecodeError, json.JSONDecodeError, RecursionError:
                _reject("malformed tool event")
            event = ToolEvent.from_dict(decoded)
            if event.kind == "result":
                terminal = True
                saw_terminal = True
            yield event
            if terminal and buffer:
                _reject("duplicate tool result")
    if buffer:
        _reject("truncated tool frame")
    if not saw_terminal:
        _reject("missing tool result")
