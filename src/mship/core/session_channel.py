"""Same-host, single-use control capabilities for #507-supervised app adapters.

This module does not launch processes or expose a network transport. Only the
supervisor mints observer claims; domain owners validate their own target/app.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import select
import socket
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO

from mship.core.run_ref import is_run_ref_segment
from mship.core.session_inputs import (
    OwnerRequest,
    SessionError,
    identifier,
    private_path,
    source_revision,
    strict_object,
)

OWNER_CONTEXT_FILE = "MSHIP_OWNER_CONTEXT_FILE"
MAX_FRAME_BYTES = 128 * 1024
CLAIM_TTL_SECONDS = 30.0
_MAX_CONNECTIONS = 32
_MAX_REPLAY_ENTRIES = 1024
_CONTEXT_FIELDS = {
    "version",
    "task",
    "repo",
    "owner_ref",
    "generation",
    "source_revision",
    "workspace_root",
    "worktree",
    "private_root",
    "socket_path",
    "secret",
    "request",
    "authorization",
}
EventSink = Callable[[dict[str, object]], None]
OwnerHandler = Callable[
    [OwnerRequest, dict[str, object], threading.Event, EventSink], dict[str, object]
]


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise SessionError("invalid", "Duplicate session input")
        result[name] = value
    return result


def _bad_constant(value: str) -> None:
    raise SessionError("invalid", "Invalid session number")


def decode_private_json(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_FRAME_BYTES:
        raise SessionError("invalid", "Session input exceeds limit")
    try:
        value = json.loads(
            raw, object_pairs_hook=_json_object, parse_constant=_bad_constant
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise SessionError("invalid", "Invalid session input") from error
    if not isinstance(value, dict):
        raise SessionError("invalid", "Invalid session input")
    return value


def _encode(value: object) -> bytes:
    try:
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (ValueError, TypeError, RecursionError) as error:
        raise SessionError("invalid", "Invalid session output") from error
    if len(raw) > MAX_FRAME_BYTES:
        raise SessionError("invalid", "Session output exceeds limit")
    return raw


def _private_directory(path: Path) -> int:
    """Pin every component without following a replacement symlink."""
    if not path.is_absolute():
        raise SessionError("invalid", "Invalid private session directory")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in {".", ".."}:
                raise SessionError("invalid", "Invalid private session directory")
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SessionError("invalid", "Session directory is not private")
        return fd
    except BaseException:
        os.close(fd)
        raise


def acquire_app_lease(platform: str, device_id: str, app_id: str) -> BinaryIO:
    """Exclude another live native/framework owner of this host's exact app.

    Lock files are never unlinked: replacing the inode would split admission.
    The fixed POSIX host namespace cannot be changed by a task's environment.
    """
    if platform not in {"android", "ios"} or any(
        not isinstance(value, str) or not value or len(value) > 1024
        for value in (device_id, app_id)
    ):
        raise SessionError("invalid", "Invalid app admission identity")
    try:
        import fcntl
    except ImportError as error:
        raise SessionError(
            "unavailable", "App ownership requires a POSIX host"
        ) from error
    try:
        root = Path("/tmp").resolve(strict=True) / f"mship-app-leases-{os.getuid()}"
        root.mkdir(mode=0o700, exist_ok=True)
        directory = _private_directory(root)
        try:
            name = hashlib.sha256(_encode([platform, device_id, app_id])).hexdigest()
            fd = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise SessionError("unknown", "App admission state is not private")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return os.fdopen(fd, "r+b")
            except BaseException:
                os.close(fd)
                raise
        finally:
            os.close(directory)
    except BlockingIOError as error:
        raise SessionError(
            "busy", "The selected app already has a live owner"
        ) from error
    except OSError as error:
        raise SessionError(
            "unavailable", "App admission state is unavailable"
        ) from error


def read_private_json(path: Path) -> dict[str, object]:
    directory = _private_directory(path.parent)
    try:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > MAX_FRAME_BYTES
            ):
                raise SessionError("invalid", "Session input is not private")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return decode_private_json(stream.read(MAX_FRAME_BYTES + 1))
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _write_receipt(root: Path, name: str, value: dict[str, object]) -> None:
    directory = _private_directory(root)
    temporary = f".{name}-{os.urandom(12).hex()}"
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            payload = memoryview(_encode(value))
            while payload:
                written = os.write(fd, payload)
                if written <= 0:
                    raise OSError("short session receipt write")
                payload = payload[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


@dataclass(frozen=True)
class OwnerContext:
    task: str
    repo: str
    owner_ref: str
    generation: str
    source_revision: str
    workspace_root: Path = field(repr=False)
    worktree: Path = field(repr=False)
    private_root: Path = field(repr=False)
    socket_path: Path = field(repr=False)
    secret: str | None = field(repr=False)
    request: OwnerRequest | None = field(default=None, repr=False)
    authorization: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.task, str)
            or not is_run_ref_segment(self.task)
            or not isinstance(self.repo, str)
            or not is_run_ref_segment(self.repo)
        ):
            raise SessionError("invalid", "Invalid task session identity")
        identifier(self.owner_ref)
        identifier(self.generation)
        source_revision(self.source_revision)
        for value in (
            self.workspace_root,
            self.worktree,
            self.private_root,
            self.socket_path,
        ):
            if not isinstance(value, Path):
                raise SessionError("invalid", "Invalid private session path")
            private_path(str(value))
        if len(os.fsencode(self.socket_path)) >= 100:
            raise SessionError("unavailable", "Private session socket path is too long")
        if self.secret is not None:
            identifier(self.secret)
            if self.authorization is not None:
                raise SessionError("invalid", "Invalid owner authorization")
        elif (
            self.request is None
            or not isinstance(self.authorization, str)
            or len(self.authorization) != 64
        ):
            raise SessionError("invalid", "Missing observer authorization")
        if self.request is not None and not isinstance(self.request, OwnerRequest):
            raise SessionError("invalid", "Invalid observer request")

    def _identity(self) -> dict[str, object]:
        return {
            "task": self.task,
            "repo": self.repo,
            "owner_ref": self.owner_ref,
            "generation": self.generation,
        }

    def _claim(self, request: OwnerRequest) -> dict[str, object]:
        return {**self._identity(), "request": request.to_private_dict()}

    def issue(self, request: OwnerRequest) -> OwnerContext:
        """Supervisor-only: mint one observer authorization, never a shared secret."""
        if self.secret is None or request.source_revision != self.source_revision:
            raise SessionError("invalid", "Owner cannot authorize this observation")
        authorization = hmac.new(
            self.secret.encode(), _encode(self._claim(request)), hashlib.sha256
        ).hexdigest()
        return replace(self, secret=None, request=request, authorization=authorization)

    def to_private_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            **self._identity(),
            "source_revision": self.source_revision,
            "workspace_root": str(self.workspace_root),
            "worktree": str(self.worktree),
            "private_root": str(self.private_root),
            "socket_path": str(self.socket_path),
            "secret": self.secret,
            "request": None if self.request is None else self.request.to_private_dict(),
            "authorization": self.authorization,
        }

    @classmethod
    def from_environ(cls) -> OwnerContext:
        value = os.environ.get(OWNER_CONTEXT_FILE)
        if value is None:
            raise SessionError("unavailable", "No supervised session context")
        try:
            data = strict_object(
                read_private_json(private_path(value)), _CONTEXT_FIELDS
            )
            if data["version"] != 1 or type(data["version"]) is not int:
                raise SessionError("invalid", "Unsupported session context")
            for key in ("task", "repo", "owner_ref", "generation", "source_revision"):
                if not isinstance(data[key], str):
                    raise SessionError("invalid", "Invalid session context")
            secret, authorization = data["secret"], data["authorization"]
            if (secret is not None and not isinstance(secret, str)) or (
                authorization is not None and not isinstance(authorization, str)
            ):
                raise SessionError("invalid", "Invalid session context")
            context = cls(
                task=data["task"],
                repo=data["repo"],
                owner_ref=data["owner_ref"],
                generation=data["generation"],
                source_revision=data["source_revision"],
                workspace_root=private_path(data["workspace_root"]),
                worktree=private_path(data["worktree"]),
                private_root=private_path(data["private_root"]),
                socket_path=private_path(data["socket_path"]),
                secret=secret,
                request=None
                if data["request"] is None
                else OwnerRequest.from_private_dict(data["request"]),
                authorization=authorization,
            )
            if any(
                os.environ.get(key) != expected
                for key, expected in (
                    ("MSHIP_TASK", context.task),
                    ("MSHIP_REPO", context.repo),
                    ("MSHIP_SOURCE_REVISION", context.source_revision),
                )
            ):
                raise SessionError(
                    "invalid", "Session execution context does not match"
                )
            return context
        except OSError as error:
            raise SessionError(
                "unknown", "Private session context is unavailable"
            ) from error

    def begin(self) -> None:
        if self.secret is None:
            raise SessionError("invalid", "Only the lifetime owner may begin a session")
        _write_receipt(
            self.private_root, "domain-started.json", {"version": 1, **self._identity()}
        )

    def ready(self) -> None:
        if self.secret is None:
            raise SessionError(
                "invalid", "Only the lifetime owner may acknowledge readiness"
            )
        _write_receipt(
            self.private_root,
            "domain-ready.json",
            {"version": 1, **self._identity(), "source_revision": self.source_revision},
        )

    def readiness_acknowledged(self) -> bool:
        try:
            receipt = read_private_json(self.private_root / "domain-ready.json")
            started = read_private_json(self.private_root / "domain-started.json")
        except FileNotFoundError:
            return False
        if started != {"version": 1, **self._identity()} or receipt != {
            "version": 1,
            **self._identity(),
            "source_revision": self.source_revision,
        }:
            raise SessionError("unknown", "Session readiness identity does not match")
        return True

    def acknowledge_source_reload(self, request: OwnerRequest, update_id: str) -> None:
        if self.secret is None or request.operation != "source-release":
            raise SessionError(
                "invalid", "Only the owner may acknowledge a source reload"
            )
        receipt = {
            "kind": "source-reloaded",
            "claim": self._claim(request),
            "update_id": update_id,
        }
        authorization = hmac.new(
            self.secret.encode(), _encode(receipt), hashlib.sha256
        ).hexdigest()
        _write_receipt(
            self.private_root,
            f"source-reload-{request.operation_ref}.json",
            {"receipt": receipt, "authorization": authorization},
        )

    def verify_source_reload(self, request: OwnerRequest, update_id: str) -> None:
        if self.secret is None or request.operation != "source-release":
            raise SessionError(
                "invalid", "Missing source reload verification authority"
            )
        expected = {
            "kind": "source-reloaded",
            "claim": self._claim(request),
            "update_id": update_id,
        }
        try:
            value = strict_object(
                read_private_json(
                    self.private_root / f"source-reload-{request.operation_ref}.json"
                ),
                {"receipt", "authorization"},
            )
        except (OSError, SessionError) as error:
            raise SessionError(
                "unknown", "Source reload was not acknowledged by the exact owner"
            ) from error
        authorization = value["authorization"]
        if (
            value["receipt"] != expected
            or not isinstance(authorization, str)
            or not authorization.isascii()
            or not hmac.compare_digest(
                hmac.new(
                    self.secret.encode(), _encode(expected), hashlib.sha256
                ).hexdigest(),
                authorization,
            )
        ):
            raise SessionError(
                "unknown", "Source reload was not acknowledged by the exact owner"
            )

    def acknowledge_capture(
        self, request: OwnerRequest, *, cancel: threading.Event
    ) -> None:
        if (
            self.secret is None
            or request.operation != "capture"
            or request.capture is None
        ):
            raise SessionError("invalid", "Only the owner may acknowledge a capture")
        from mship.core.capture import KIND_FILENAMES

        artifacts = []
        total = 0
        directory = _private_directory(request.capture.directory)
        try:
            for kind in request.capture.kinds:
                for name in KIND_FILENAMES[kind]:
                    try:
                        fd = os.open(
                            name,
                            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                            dir_fd=directory,
                        )
                    except FileNotFoundError:
                        continue
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise SessionError(
                                "unknown", "Capture output is not a regular owned file"
                            )
                        if info.st_size == 0:
                            continue
                        total += info.st_size
                        if total + 32 * 1024 > 256 * 1024 * 1024:
                            raise SessionError(
                                "unavailable",
                                "Capture output exceeds the transfer limit",
                            )
                        digest = hashlib.sha256()
                        remaining = info.st_size
                        while remaining:
                            if cancel.is_set():
                                raise SessionError(
                                    "cancelled", "Capture acknowledgement cancelled"
                                )
                            chunk = os.read(fd, min(64 * 1024, remaining))
                            if not chunk:
                                raise SessionError(
                                    "unknown",
                                    "Capture output ended during acknowledgement",
                                )
                            digest.update(chunk)
                            remaining -= len(chunk)
                        after = os.fstat(fd)
                        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                            info.st_size,
                            info.st_mtime_ns,
                            info.st_ctime_ns,
                        ):
                            raise SessionError(
                                "unknown",
                                "Capture output changed during acknowledgement",
                            )
                        artifacts.append(
                            {
                                "kind": kind,
                                "name": name,
                                "size": info.st_size,
                                "sha256": digest.hexdigest(),
                            }
                        )
                        break
                    finally:
                        os.close(fd)
                else:
                    raise SessionError(
                        "unavailable", "Capture omitted a requested artifact"
                    )
        finally:
            os.close(directory)
        receipt = {
            "kind": "capture-complete",
            "claim": self._claim(request),
            "artifacts": artifacts,
        }
        authorization = hmac.new(
            self.secret.encode(), _encode(receipt), hashlib.sha256
        ).hexdigest()
        _write_receipt(
            request.capture.directory,
            "owner-capture-receipt.json",
            {"receipt": receipt, "authorization": authorization},
        )

    def verify_capture(self, request: OwnerRequest) -> dict[str, tuple[int, str]]:
        if self.secret is None or request.capture is None:
            raise SessionError("invalid", "Missing capture verification authority")
        from mship.core.capture import KIND_FILENAMES

        value = strict_object(
            read_private_json(request.capture.directory / "owner-capture-receipt.json"),
            {"receipt", "authorization"},
        )
        receipt = strict_object(value["receipt"], {"kind", "claim", "artifacts"})
        authorization = value["authorization"]
        if (
            receipt["kind"] != "capture-complete"
            or receipt["claim"] != self._claim(request)
            or not isinstance(authorization, str)
            or not hmac.compare_digest(
                hmac.new(
                    self.secret.encode(), _encode(dict(receipt)), hashlib.sha256
                ).hexdigest(),
                authorization,
            )
        ):
            raise SessionError(
                "unknown", "Capture was not acknowledged by the exact owner"
            )
        artifacts = receipt["artifacts"]
        if not isinstance(artifacts, list) or len(artifacts) != len(
            request.capture.kinds
        ):
            raise SessionError(
                "unknown", "Capture acknowledgement has invalid artifacts"
            )
        manifest = {}
        observed_kinds = set()
        for item in artifacts:
            item = strict_object(item, {"kind", "name", "size", "sha256"})
            kind, name, size, digest = (
                item["kind"],
                item["name"],
                item["size"],
                item["sha256"],
            )
            if (
                not isinstance(kind, str)
                or kind not in request.capture.kinds
                or kind in observed_kinds
                or name not in KIND_FILENAMES[kind]
                or type(size) is not int
                or size <= 0
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise SessionError(
                    "unknown", "Capture acknowledgement has invalid artifacts"
                )
            observed_kinds.add(kind)
            manifest[name] = (size, digest)
        return manifest

    def finish(self, *, cleanup_known: bool) -> None:
        if self.secret is None or type(cleanup_known) is not bool:
            raise SessionError(
                "invalid", "Only the lifetime owner may complete a session"
            )
        _write_receipt(
            self.private_root,
            "domain-cleanup.json",
            {"version": 1, **self._identity(), "cleanup_known": cleanup_known},
        )

    def cleanup_acknowledged(self) -> bool:
        try:
            started = read_private_json(self.private_root / "domain-started.json")
        except FileNotFoundError:
            try:
                read_private_json(self.private_root / "domain-ready.json")
            except FileNotFoundError:
                return True  # No domain action was admitted.
            return False  # Readiness without a started receipt is not proof of cleanup.
        expected = {"version": 1, **self._identity()}
        if started != expected:
            return False
        try:
            return read_private_json(self.private_root / "domain-cleanup.json") == {
                **expected,
                "cleanup_known": True,
            }
        except OSError, SessionError:
            return False


class _FrameReader:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.pending = bytearray()

    def receive(
        self, cancel: threading.Event, *, deadline: float | None = None
    ) -> dict[str, object]:
        while not cancel.is_set():
            end = self.pending.find(b"\n")
            if end >= 0:
                if end > MAX_FRAME_BYTES:
                    raise SessionError("invalid", "Session frame exceeds limit")
                line = bytes(self.pending[:end])
                del self.pending[: end + 1]
                return decode_private_json(line)
            if len(self.pending) > MAX_FRAME_BYTES:
                raise SessionError("invalid", "Session frame exceeds limit")
            if deadline is not None and time.monotonic() >= deadline:
                raise SessionError("unavailable", "Session command timed out")
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise SessionError("unknown", "Session connection ended")
            self.pending.extend(chunk)
        raise SessionError("cancelled", "Session observation cancelled")


def _send(sock: socket.socket, value: dict[str, object]) -> None:
    sock.sendall(_encode(value) + b"\n")


class OwnerClient:
    def __init__(self, context: OwnerContext):
        self.context = context

    def call(
        self,
        operation: str,
        payload: dict[str, object],
        *,
        cancel_event: threading.Event | None = None,
        event_sink: EventSink | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]:
        context = self.context
        request = context.request
        if (
            context.secret is not None
            or request is None
            or request.operation != operation
            or context.authorization is None
            or not isinstance(payload, dict)
        ):
            raise SessionError(
                "invalid", "Observer is not authorized for this operation"
            )
        cancel = cancel_event if cancel_event is not None else threading.Event()
        deadline = None if timeout is None else time.monotonic() + timeout
        if cancel.is_set():
            raise SessionError("cancelled", "Session observation cancelled")
        try:
            directory = _private_directory(context.socket_path.parent)
            os.close(directory)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.2)
                sock.connect(str(context.socket_path))
                _send(
                    sock,
                    {
                        "claim": context._claim(request),
                        "authorization": context.authorization,
                        "payload": payload,
                    },
                )
                reader = _FrameReader(sock)
                while True:
                    reply = reader.receive(cancel, deadline=deadline)
                    if set(reply) != {"kind", "value"}:
                        raise SessionError("invalid", "Invalid owner response")
                    kind, value = reply["kind"], reply["value"]
                    if kind == "error":
                        if not isinstance(value, str):
                            raise SessionError("invalid", "Invalid owner error")
                        raise SessionError(value)
                    if not isinstance(value, dict):
                        raise SessionError("invalid", "Invalid owner result")
                    if kind == "result":
                        return value
                    if kind != "event":
                        raise SessionError("invalid", "Invalid owner response")
                    if event_sink is not None:
                        event_sink(value)
        except OSError as error:
            raise SessionError(
                "unknown", "The exact session owner is unavailable"
            ) from error


def serve_owner(
    context: OwnerContext,
    handle: OwnerHandler,
    stop: threading.Event,
    *,
    source_revision: Callable[[], str] | None = None,
) -> None:
    if context.secret is None:
        raise SessionError("invalid", "An observer cannot become the session owner")
    current_source = source_revision or (lambda: context.source_revision)
    seen: dict[str, float] = {}
    lock = threading.Lock()
    admitted = threading.BoundedSemaphore(_MAX_CONNECTIONS)
    clients: dict[threading.Thread, tuple[socket.socket, threading.Event]] = {}

    def serve_connection(sock: socket.socket, cancel: threading.Event) -> None:
        monitor_stop = threading.Event()
        monitor: threading.Thread | None = None
        cleanup_requested = False
        try:
            reader = _FrameReader(sock)
            body = strict_object(
                reader.receive(cancel, deadline=time.monotonic() + 5),
                {"claim", "authorization", "payload"},
            )
            if reader.pending:
                raise SessionError("invalid", "Unexpected observer request data")
            claim = strict_object(
                body["claim"], {"task", "repo", "owner_ref", "generation", "request"}
            )
            request = OwnerRequest.from_private_dict(claim["request"])
            authorization = body["authorization"]
            if not isinstance(authorization, str) or len(authorization) != 64:
                raise SessionError("invalid", "Invalid observer authorization")
            expected = hmac.new(
                context.secret.encode(), _encode(dict(claim)), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, authorization):
                raise SessionError("invalid", "Invalid observer authorization")
            if any(claim[key] != value for key, value in context._identity().items()):
                raise SessionError("invalid", "Observer belongs to another session")
            if not isinstance(body["payload"], dict):
                raise SessionError("invalid", "Invalid session request")
            now = time.time()
            with lock:
                for reference, expiry in tuple(seen.items()):
                    if expiry < now:
                        del seen[reference]
                if (
                    not now <= request.expires_at <= now + CLAIM_TTL_SECONDS + 5
                    or request.operation_ref in seen
                    or (
                        request.operation != "cleanup"
                        and request.source_revision != current_source()
                    )
                ):
                    raise SessionError("invalid", "Stale observer authorization")
                if len(seen) >= _MAX_REPLAY_ENTRIES:
                    raise SessionError("busy", "Session admission is full")
                seen[request.operation_ref] = request.expires_at
                cleanup_requested = request.operation == "cleanup"

            def monitor_disconnect() -> None:
                while not monitor_stop.is_set() and not cancel.is_set():
                    try:
                        ready, _, _ = select.select([sock], [], [], 0.1)
                        if ready:
                            # No further client frames are allowed on this connection.
                            cancel.set()
                            return
                    except OSError, ValueError:
                        cancel.set()
                        return

            monitor = threading.Thread(target=monitor_disconnect, daemon=True)
            monitor.start()

            def emit(value: dict[str, object]) -> None:
                if cancel.is_set() or stop.is_set():
                    raise SessionError("cancelled", "Session observation cancelled")
                _send(sock, {"kind": "event", "value": value})

            result = handle(request, body["payload"], cancel, emit)
            if cancel.is_set():
                raise SessionError("cancelled", "Session observation cancelled")
            if request.operation == "capture":
                context.acknowledge_capture(request, cancel=cancel)
            if request.operation == "source-release":
                payload = strict_object(body["payload"], {"update_id"})
                update_id = payload["update_id"]
                if not isinstance(update_id, str) or result != {
                    "update_id": update_id,
                    "stage": "reloaded",
                }:
                    raise SessionError("unknown", "Source reload completion is invalid")
                context.acknowledge_source_reload(request, update_id)
            _send(sock, {"kind": "result", "value": result})
        except SessionError as error:
            try:
                _send(sock, {"kind": "error", "value": error.code})
            except OSError:
                pass
        except Exception:
            try:
                _send(sock, {"kind": "error", "value": "unknown"})
            except OSError:
                pass
        finally:
            # End the owner only after its final cleanup reply has been attempted.
            if cleanup_requested:
                stop.set()
            cancel.set()
            monitor_stop.set()
            if monitor is not None:
                monitor.join(timeout=1)
            sock.close()
            with lock:
                clients.pop(threading.current_thread(), None)
            admitted.release()

    directory = _private_directory(context.socket_path.parent)
    os.close(directory)
    identity: tuple[int, int] | None = None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(context.socket_path))
            info = context.socket_path.stat(follow_symlinks=False)
            identity = (info.st_dev, info.st_ino)
            os.chmod(context.socket_path, 0o600)
            listener.listen(_MAX_CONNECTIONS)
            context.ready()
            listener.settimeout(0.1)
            while not stop.is_set():
                try:
                    sock, _ = listener.accept()
                except socket.timeout:
                    continue
                sock.settimeout(0.2)
                if not admitted.acquire(blocking=False):
                    sock.close()
                    continue
                cancel = threading.Event()
                thread = threading.Thread(
                    target=serve_connection, args=(sock, cancel), daemon=True
                )
                with lock:
                    clients[thread] = (sock, cancel)
                thread.start()
    finally:
        with lock:
            pending = tuple(clients.items())
            for _, cancel in clients.values():
                cancel.set()
        for _, (sock, _) in pending:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        deadline = time.monotonic() + 3
        for thread, _ in pending:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if identity is not None:
            try:
                info = context.socket_path.stat(follow_symlinks=False)
                if (info.st_dev, info.st_ino) == identity and stat.S_ISSOCK(
                    info.st_mode
                ):
                    context.socket_path.unlink()
            except FileNotFoundError:
                pass
        if any(thread.is_alive() for thread, _ in pending):
            raise SessionError("unknown", "Session observation cleanup is incomplete")
