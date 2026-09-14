"""Task-bound supervised subprocess operations.

Durable records are evidence, never authority to signal a PID after restart.
Only an in-memory operation owns a process group.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import selectors
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mship.core.log import LogEntry, format_log_entry
from mship.core.remote_tool import ToolContext, ToolEvent, ToolRequest, ToolResult
from mship.core.session_channel import CLAIM_TTL_SECONDS, OWNER_CONTEXT_FILE, OwnerClient, OwnerContext
from mship.core.session_inputs import CaptureGrant, OwnerRequest, SessionError, identifier, source_revision as validate_source_revision
from mship.core.session_runtime import SessionPreparation, prepare_session_install
from mship.util.shell import (
    ShellCancellationUnsupported,
    ShellRunner,
    _owned_process_exited,
    _terminate_owned_process_group,
    ensure_cancellable_shell_supported,
    tool_runtime_environment,
)

_MAX_CHUNK = 16 * 1024
_DELIVERY_QUEUE = 32
_METADATA_LIMIT = 64 * 1024
_INDEX_LIMIT = 64 * 1024
_OPERATION_DIR = "remote-tool-operations"
_TERMINAL = frozenset(
    {
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
    }
)


@dataclass(frozen=True)
class _PrivateInput:
    name: str
    device: int
    inode: int


@dataclass
class _Operation:
    owner_ref: str
    generation: str
    context: ToolContext
    request: ToolRequest
    record_path: Path
    stdout_path: Path
    stderr_path: Path
    indexed: bool
    root_path: Path
    root_fd: int | None
    storage_fd: int | None = None
    output_fds: dict[str, int] = field(default_factory=dict)
    private_inputs: dict[str, _PrivateInput] = field(default_factory=dict)
    proc: subprocess.Popen[bytes] | None = None
    delivery: queue.Queue[ToolEvent] = field(
        default_factory=lambda: queue.Queue(_DELIVERY_QUEUE)
    )
    stop: threading.Event = field(default_factory=threading.Event)
    completed: threading.Event = field(default_factory=threading.Event)
    collection_done: threading.Event = field(default_factory=threading.Event)
    cleanup_lock: threading.Lock = field(default_factory=threading.Lock)
    cleanup_complete: bool = False
    cleanup_error: bool = False
    reason: str | None = None
    result: ToolResult | None = None
    publish_result: Callable[[ToolResult], ToolResult] | None = None
    worker: threading.Thread | None = None
    parent_done: threading.Event | None = None
    observers: dict[threading.Event, threading.Event] = field(default_factory=dict)
    observer_lock: threading.Lock = field(default_factory=threading.Lock)
    session_context: OwnerContext | None = None
    session_preparation: SessionPreparation | None = None
    capability_root: Path | None = None
    socket_directory: Path | None = None
    socket_directory_identity: tuple[int, int] | None = None
    capability_identity: tuple[int, int] | None = None
    ready_announced: bool = False
    source_update_ref: str | None = None

    def request_stop(self, reason: str) -> None:
        """Serialize teardown admission with a child's final check and spawn."""
        with self.observer_lock:
            self.reason = reason
            self.stop.set()

    def terminate(self) -> None:
        """Synchronously reap the exact owned group before reporting cleanup."""
        self.cancel_observers()
        with self.observer_lock, self.cleanup_lock:
            if self.cleanup_complete:
                return
            if self.proc is None:
                self.cleanup_complete = True
                return
            try:
                if self.indexed and self.session_context is not None and not _owned_process_exited(self.proc):
                    self._graceful_domain_cleanup()
                _terminate_owned_process_group(self.proc)
            except Exception:
                self.cleanup_error = True
                raise
            self.cleanup_complete = True

    def check_capability_root(self) -> None:
        if self.capability_root is None:
            return
        info = self.capability_root.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != self.capability_identity):
            raise OSError("private session root was replaced")

    def _graceful_domain_cleanup(self) -> None:
        """Let the domain acknowledge external cleanup before signalling its group."""
        assert self.proc is not None and self.session_context is not None
        deadline = time.monotonic() + 5
        try:
            self.check_capability_root()
            request = OwnerRequest(
                operation_ref=secrets.token_urlsafe(24), operation="cleanup",
                source_revision=self.session_context.source_revision,
                expires_at=time.time() + CLAIM_TTL_SECONDS,
            )
            OwnerClient(self.session_context.issue(request)).call("cleanup", {}, timeout=5)
        except (OSError, SessionError):
            if not _owned_process_exited(self.proc):
                # Startup may not have opened its private listener yet. Signal the
                # owner alone so its finally block can still use its live children.
                os.kill(self.proc.pid, signal.SIGTERM)
        while time.monotonic() < deadline and not _owned_process_exited(self.proc):
            time.sleep(0.02)

    def leader_exited(self) -> bool:
        with self.cleanup_lock:
            if self.cleanup_complete:
                return True
            return self.proc is not None and _owned_process_exited(self.proc)

    def close_context(self) -> None:
        with self.cleanup_lock:
            for fd in (self.root_fd, self.storage_fd, *self.output_fds.values()):
                if fd is not None:
                    os.close(fd)
            self.root_fd = self.storage_fd = None
            self.output_fds.clear()

    def cancel_observers(self) -> tuple[threading.Event, ...]:
        with self.observer_lock:
            pairs = tuple(self.observers.items())
            for cancellation, _ in pairs:
                cancellation.set()
        return tuple(done for _, done in pairs)


class ToolOperationRegistry:
    """Workspace-local owner for live tool process groups and private evidence."""

    def __init__(self, workspace_root: Path, *, state_dir: Path | None = None):
        self._workspace_root = Path(workspace_root).resolve()
        self._state_dir = (
            Path(state_dir)
            if state_dir is not None
            else self._workspace_root / ".mothership"
        )
        workspace_id = hashlib.sha256(
            str(self._workspace_root).encode("utf-8")
        ).hexdigest()
        self._root = self._state_dir / _OPERATION_DIR / workspace_id
        self._lock = threading.RLock()
        self._operations: dict[tuple[str, str], _Operation] = {}

    def run(
        self,
        request: ToolRequest,
        context: ToolContext,
        *,
        cancel_event: threading.Event | None = None,
        spawn: Callable | None = None,
        publish_result: Callable[[ToolResult], ToolResult] | None = None,
        session: SessionPreparation | None = None,
    ) -> Generator[ToolEvent, None, None]:
        invalid = self._validate_launch(request, context)
        if invalid is not None:
            yield _result_event(invalid)
            return
        try:
            ensure_cancellable_shell_supported()
        except ShellCancellationUnsupported:
            yield _result_event(ToolResult(status="unsupported"))
            return

        operation: _Operation | None = None
        rejection: ToolResult | None = None
        if cancel_event is not None and cancel_event.is_set():
            rejection = ToolResult(status="cancelled")
        else:
            with self._lock:
                if cancel_event is not None and cancel_event.is_set():
                    rejection = ToolResult(status="cancelled")
                else:
                    admission = self._admission_status_locked(context.task)
                    if admission != "available":
                        rejection = ToolResult(status=admission)
                    else:
                        try:
                            operation = self._new_operation(
                                request,
                                context,
                                indexed=True,
                                publish_result=publish_result,
                                session=session,
                            )
                            self._write_record(operation, "starting")
                            self._set_index(operation)
                            self._journal(operation, "starting")
                        except (OSError, SessionError):
                            rejection = ToolResult(status="evidence_error")
                        else:
                            self._operations[
                                (operation.owner_ref, operation.generation)
                            ] = operation
        if rejection is not None:
            if operation is not None:
                operation.close_context()
            yield _result_event(rejection)
            return
        if operation is None:
            yield _result_event(ToolResult(status="unknown"))
            return
        failure = self._launch(operation, spawn, cancel_event)
        if failure is not None:
            self._finish(operation, failure)
            yield _result_event(operation.result)
            return
        self._start_worker(operation, cancel_event)
        yield from self._consume(operation)

    def observe(
        self,
        request: ToolRequest,
        *,
        cancel_event: threading.Event | None = None,
        spawn: Callable | None = None,
        session: SessionPreparation | None = None,
        on_session_prepared: Callable[[OwnerContext], None] | None = None,
        source_update_id: str | None = None,
    ) -> Generator[ToolEvent, None, None]:
        if request.owner_ref is None or request.generation is None:
            yield _result_event(ToolResult(status="invalid"))
            return
        if not request.argv:
            result = self.status(
                task=request.task,
                repo=request.repo,
                owner_ref=request.owner_ref,
                generation=request.generation,
            )
            if (
                request.source_revision is not None
                and result.source_revision != request.source_revision
            ):
                result = ToolResult(status="invalid")
            yield _result_event(result)
            return
        unavailable_status = "unknown"
        with self._lock:
            parent = self._operations.get((request.owner_ref, request.generation))
            if (
                parent is None
                or parent.completed.is_set()
                or parent.stop.is_set()
                or parent.collection_done.is_set()
                or parent.proc is None
                or not self._matches_parent(request, parent)
            ):
                unavailable = True
            elif parent.source_update_ref != source_update_id:
                unavailable = True
                unavailable_status = "busy" if parent.source_update_ref is not None else "invalid"
            else:
                unavailable = False
                cancelled = threading.Event()
                done = threading.Event()
                with parent.observer_lock:
                    if (
                        parent.completed.is_set()
                        or parent.stop.is_set()
                        or parent.collection_done.is_set()
                    ):
                        unavailable = True
                    else:
                        parent.observers[cancelled] = done
        if unavailable:
            yield _result_event(ToolResult(status=unavailable_status))
            return
        try:
            operation = self._new_operation(
                request, parent.context, indexed=False, parent=parent, session=session
            )
        except (OSError, SessionError):
            done.set()
            with parent.observer_lock:
                parent.observers.pop(cancelled, None)
            yield _result_event(ToolResult(status="unknown"))
            return
        operation.parent_done = done
        if on_session_prepared is not None and operation.session_context is not None:
            try:
                on_session_prepared(operation.session_context)
            except Exception:
                self._finish(operation, "evidence_error")
                with parent.observer_lock:
                    parent.observers.pop(cancelled, None)
                yield _result_event(operation.result)
                return
        combined_cancel = _combined_cancel(cancel_event, cancelled)
        failure = self._launch(operation, spawn, combined_cancel, parent=parent)
        if failure is not None:
            self._finish(operation, failure)
            if done.is_set():
                with parent.observer_lock:
                    parent.observers.pop(cancelled, None)
            yield _result_event(operation.result)
            return
        self._start_worker(operation, combined_cancel)
        try:
            yield from self._consume(operation)
        finally:
            if done.is_set():
                with parent.observer_lock:
                    parent.observers.pop(cancelled, None)

    def status(
        self, *, task: str, repo: str, owner_ref: str, generation: str
    ) -> ToolResult:
        if not _safe_identifier(owner_ref) or not _safe_identifier(generation):
            return ToolResult(status="invalid")
        with self._lock:
            operation = self._operations.get((owner_ref, generation))
            if operation is not None:
                if operation.context.task != task or operation.context.repo != repo:
                    return ToolResult(status="invalid")
                if operation.cleanup_error or operation.reason == "unknown":
                    return ToolResult(
                        status="unknown",
                        owner_ref=owner_ref,
                        generation=generation,
                        source_revision=operation.context.source_revision,
                    )
                if operation.source_update_ref is not None:
                    return ToolResult(
                        status="busy", owner_ref=owner_ref, generation=generation,
                        source_revision=operation.context.source_revision,
                    )
                return operation.result or _running_result(operation)
            record = self._read_record(task, owner_ref)
        if record is None or record.get("generation") != generation:
            return ToolResult(status="unknown")
        if record.get("task") != task or record.get("repo") != repo:
            return ToolResult(status="invalid")
        status = record.get("status")
        if status not in _TERMINAL:
            return ToolResult(
                status="unknown",
                owner_ref=owner_ref,
                generation=generation,
                source_revision=_text_or_none(record.get("source_revision")),
            )
        try:
            return ToolResult(
                status=status,
                exit_code=_exit_or_none(record.get("exit_code")),
                owner_ref=owner_ref,
                generation=generation,
                source_revision=_text_or_none(record.get("source_revision")),
            )
        except ValueError:
            return ToolResult(status="unknown")

    def session_for_owner(
        self, *, task: str, repo: str, owner_ref: str, generation: str
    ) -> tuple[OwnerContext, dict[str, str]] | None:
        """Return only a live parent's private inputs to internal server adapters."""
        with self._lock:
            parent = self._operations.get((owner_ref, generation))
            if (parent is None or parent.session_context is None
                    or parent.completed.is_set() or parent.stop.is_set()
                    or parent.collection_done.is_set()):
                return None
            request = ToolRequest(
                task=task, repo=repo, argv=(), preparation="observe",
                owner_ref=owner_ref, generation=generation,
            )
            if not self._matches_parent(request, parent):
                return None
            try:
                self._private_input_environment(parent)
                parent.check_capability_root()
            except OSError:
                return None
            return parent.session_context, dict(parent.request.input_files)

    def reserve_source_update(
        self, request: ToolRequest, update_id: str
    ) -> tuple[ToolContext, OwnerContext, dict[str, str]]:
        identifier(update_id)
        with self._lock:
            parent = self._source_parent(request)
            if not self._matches_parent(request, parent) or not parent.ready_announced:
                raise SessionError("unavailable", "Framework owner is not ready for this source")
            with parent.observer_lock:
                if parent.source_update_ref is not None:
                    raise SessionError("busy", "Framework source update is already reserved")
                self._private_input_environment(parent)
                parent.check_capability_root()
                parent.source_update_ref = update_id
                observations = tuple(parent.observers.items())
                for cancelled, _ in observations:
                    cancelled.set()
        deadline = time.monotonic() + 10
        if any(not done.wait(max(0, deadline - time.monotonic())) for _, done in observations):
            self.release_source_update(request, update_id, unknown=True)
            raise SessionError("unknown", "Framework observations did not acknowledge cleanup")
        with self._lock:
            parent = self._source_parent(request, update_id)
            assert parent.session_context is not None
            return parent.context, parent.session_context, dict(parent.request.input_files)

    def _source_parent(
        self, request: ToolRequest, update_id: str | None = None
    ) -> _Operation:
        parent = self._operations.get((request.owner_ref, request.generation))
        if (request.preparation != "observe" or parent is None
                or parent.context.task != request.task or parent.context.repo != request.repo
                or parent.session_context is None or parent.session_preparation is None
                or parent.session_preparation.owner_kind != "flutter"
                or parent.completed.is_set() or parent.stop.is_set()
                or parent.collection_done.is_set() or parent.cleanup_error
                or (update_id is not None and parent.source_update_ref != update_id)):
            raise SessionError("unknown", "The exact framework source owner is unavailable")
        self._check_root(parent)
        parent.check_capability_root()
        return parent

    def commit_source_update(
        self, request: ToolRequest, update_id: str, *,
        source_revision: str, profile_revision: str,
    ) -> OwnerContext:
        validate_source_revision(source_revision)
        if (not isinstance(profile_revision, str) or len(profile_revision) != 64
                or any(character not in "0123456789abcdef" for character in profile_revision)):
            raise SessionError("invalid", "Invalid committed profile identity")
        with self._lock:
            parent = self._source_parent(request, update_id)
            if parent.context.source_revision != request.source_revision:
                raise SessionError("invalid", "Source context was already advanced")
            self._private_input_environment(parent)
            assert parent.session_context is not None
            next_context = replace(parent.context, source_revision=source_revision)
            next_owner = replace(parent.session_context, source_revision=source_revision, request=None)
            files = dict(parent.request.input_files)
            for name in ("MSHIP_TARGET_REQUEST_FILE", "MSHIP_TARGET_CONTEXT_FILE"):
                value = json.loads(files[name])
                value["backend_revision"] = source_revision
                value["profile_revision"] = profile_revision
                files[name] = json.dumps(value, separators=(",", ":"))
            private_updates = {
                name: files[name]
                for name in ("MSHIP_TARGET_REQUEST_FILE", "MSHIP_TARGET_CONTEXT_FILE")
            }
            private_updates[OWNER_CONTEXT_FILE] = json.dumps(
                next_owner.to_private_dict(), separators=(",", ":")
            )
            try:
                with self._operation_directory(parent) as directory_fd:
                    for name, content in private_updates.items():
                        previous = parent.private_inputs[name]
                        self._publish_json(directory_fd, previous.name, content.encode("utf-8"))
                        fd = os.open(previous.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                        try:
                            info = self._check_private_file(fd)
                            parent.private_inputs[name] = _PrivateInput(previous.name, info.st_dev, info.st_ino)
                        finally:
                            os.close(fd)
                parent.context = next_context
                parent.session_context = next_owner
                parent.request = replace(parent.request, source_revision=source_revision, input_files=files)
                parent.session_preparation = replace(
                    parent.session_preparation, sealed_context=files["MSHIP_TARGET_CONTEXT_FILE"]
                )
                self._write_record(parent, "running")
            except (OSError, KeyError, ValueError):
                parent.request_stop("unknown")
                raise SessionError("unknown", "Source authorization commit is incomplete") from None
            return next_owner

    def release_source_update(
        self, request: ToolRequest, update_id: str, *, unknown: bool = False
    ) -> None:
        with self._lock:
            parent = self._operations.get((request.owner_ref, request.generation))
            if (parent is None or parent.context.task != request.task
                    or parent.context.repo != request.repo or parent.source_update_ref != update_id):
                raise SessionError("unknown", "The source reservation is no longer owned")
            with parent.observer_lock:
                if unknown:
                    parent.reason = "unknown"
                    parent.stop.set()
                else:
                    parent.source_update_ref = None

    def admission_status(
        self, task: str
    ) -> Literal["available", "busy", "unknown", "evidence_error"]:
        """Distinguish live contention from unvalidated durable ownership."""
        if not isinstance(task, str) or not task:
            return "unknown"
        with self._lock:
            return self._admission_status_locked(task)

    def _admission_status_locked(
        self, task: str
    ) -> Literal["available", "busy", "unknown", "evidence_error"]:
        if any(
            op.context.task == task and (op.cleanup_error or op.reason == "unknown")
            for op in self._operations.values()
        ):
            return "unknown"
        try:
            active = self._read_active(task)
        except OSError, ValueError, TypeError, RecursionError:
            return "evidence_error"
        if active is None:
            return (
                "busy"
                if any(op.context.task == task for op in self._operations.values())
                else "available"
            )
        operation = self._operations.get((active["owner_ref"], active["generation"]))
        if (
            operation is None
            or operation.context.task != task
            or operation.cleanup_error
        ):
            return "unknown"
        return "busy" if operation.reason != "unknown" else "unknown"

    def _validate_launch(
        self, request: ToolRequest, context: ToolContext
    ) -> ToolResult | None:
        if (
            request.preparation == "observe"
            or request.task != context.task
            or request.repo != context.repo
        ):
            return ToolResult(status="invalid")
        if (
            request.source_revision is not None
            and request.source_revision != context.source_revision
        ):
            return ToolResult(status="invalid")
        try:
            self._resolve_cwd(context.worktree, request.cwd)
        except OSError, ValueError:
            return ToolResult(status="invalid")
        return None

    def _matches_parent(self, request: ToolRequest, parent: _Operation) -> bool:
        if (
            request.preparation != "observe"
            or request.task != parent.context.task
            or request.repo != parent.context.repo
        ):
            return False
        if request.source_revision not in {None, parent.context.source_revision}:
            return False
        try:
            if parent.leader_exited():
                return False
            self._check_root(parent)
            self._resolve_cwd(parent.root_path, request.cwd)
        except OSError, ValueError, RuntimeError:
            return False
        return True

    def _new_operation(
        self,
        request: ToolRequest,
        context: ToolContext,
        *,
        indexed: bool,
        parent: _Operation | None = None,
        publish_result: Callable[[ToolResult], ToolResult] | None = None,
        session: SessionPreparation | None = None,
    ) -> _Operation:
        owner = parent.owner_ref if parent is not None else secrets.token_urlsafe(24)
        generation = (
            parent.generation if parent is not None else secrets.token_urlsafe(24)
        )
        record = self._record_path(context.task, owner)
        stem = record.with_suffix("")
        if not indexed:
            stem = Path(f"{stem}.observer-{secrets.token_urlsafe(12)}")
            record = Path(f"{stem}.json")
        if parent is None:
            root = context.worktree.resolve(strict=True)
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        else:
            with parent.cleanup_lock:
                if parent.root_fd is None:
                    raise OSError("parent context is unavailable")
                root, root_fd = parent.root_path, os.dup(parent.root_fd)
        try:
            with self._open_storage_dir(record.parent, create=True) as directory_fd:
                storage_fd = os.dup(directory_fd)
        except OSError:
            os.close(root_fd)
            raise
        operation = _Operation(
            owner_ref=owner,
            generation=generation,
            context=context,
            request=request,
            record_path=record,
            stdout_path=Path(f"{stem}.stdout"),
            stderr_path=Path(f"{stem}.stderr"),
            indexed=indexed,
            root_path=root,
            root_fd=root_fd,
            storage_fd=storage_fd,
            publish_result=publish_result,
            session_preparation=session,
        )
        if session is not None:
            try:
                self._prepare_session(operation, parent)
            except BaseException:
                self._remove_socket_directory(operation)
                operation.close_context()
                raise
        return operation

    def _prepare_session(self, operation: _Operation, parent: _Operation | None) -> None:
        preparation = operation.session_preparation
        assert preparation is not None
        if parent is not None and (
            parent.session_context is None
            or parent.session_preparation is None
            or parent.session_preparation.owner_kind != preparation.owner_kind
        ):
            raise SessionError("invalid", "No matching domain owner")
        with self._operation_directory(operation) as directory_fd:
            name = f"{operation.record_path.stem}.session"
            os.mkdir(name, mode=0o700, dir_fd=directory_fd)
            operation.capability_root = (operation.record_path.parent / name).resolve(strict=True)
            info = operation.capability_root.stat(follow_symlinks=False)
            operation.capability_identity = (info.st_dev, info.st_ino)
            os.fsync(directory_fd)
        root = operation.capability_root
        install = (
            None if preparation.install is None
            else prepare_session_install(preparation.result_store, preparation.install, root)
        )
        capture = None
        if preparation.capture_kinds is not None:
            capture_path = root / "capture"
            capture_path.mkdir(mode=0o700)
            capture = CaptureGrant(capture_path, preparation.capture_kinds,
                                   preparation.capture_platform or "")
        request = OwnerRequest(
            operation_ref=secrets.token_urlsafe(24),
            operation=preparation.operation,
            source_revision=operation.context.source_revision,
            expires_at=time.time() + CLAIM_TTL_SECONDS,
            install=install,
            capture=capture,
        )
        files = dict(operation.request.input_files)
        if parent is None:
            socket_directory = Path(tempfile.mkdtemp(prefix="mso-")).resolve(strict=True)
            operation.socket_directory = socket_directory
            info = socket_directory.stat(follow_symlinks=False)
            operation.socket_directory_identity = (info.st_dev, info.st_ino)
            operation.session_context = OwnerContext(
                task=operation.context.task, repo=operation.context.repo,
                owner_ref=operation.owner_ref, generation=operation.generation,
                source_revision=operation.context.source_revision,
                workspace_root=self._workspace_root, worktree=operation.root_path,
                private_root=root, socket_path=socket_directory / "owner.sock",
                secret=secrets.token_urlsafe(32), request=request,
            )
            if preparation.sealed_context is None:
                raise SessionError("invalid", "Missing server-selected target")
            files["MSHIP_TARGET_CONTEXT_FILE"] = preparation.sealed_context
        else:
            assert parent.session_context is not None
            # Verify the parent's private files before authorizing a new child.
            self._private_input_environment(parent)
            parent.check_capability_root()
            operation.session_context = parent.session_context.issue(request)
            for key in ("MSHIP_TARGET_CONTEXT_FILE", "MSHIP_TARGET_BINDINGS_FILE"):
                if key not in parent.request.input_files:
                    raise SessionError("unknown", "Parent target context is missing")
                files[key] = parent.request.input_files[key]
        operation.request = replace(operation.request, input_files=files)

    @staticmethod
    def _remove_socket_directory(operation: _Operation) -> None:
        directory = operation.socket_directory
        if directory is None:
            return
        info = directory.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino)
                != operation.socket_directory_identity):
            raise OSError("owner socket directory was replaced")
        # Never recursively delete: an unexpected entry preserves uncertainty.
        socket_path = directory / "owner.sock"
        try:
            socket_info = socket_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(socket_info.st_mode):
                raise OSError("owner socket was replaced")
            socket_path.unlink()
        directory.rmdir()
        operation.socket_directory = None

    def _launch(
        self,
        operation: _Operation,
        spawn: Callable | None,
        cancel_event: threading.Event | _CombinedCancel | None,
        *,
        parent: _Operation | None = None,
    ) -> str | None:
        try:
            self._prepare_inputs(operation)
            self._prepare_output(operation)
        except OSError:
            return "evidence_error"
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        try:
            argv = self._wrapped_argv(
                operation.request.argv, operation.context.env_runner
            )
            with self._pinned_cwd(operation) as cwd:
                if parent is None:
                    if cancel_event is not None and cancel_event.is_set():
                        return "cancelled"
                    proc = (spawn or ShellRunner().spawn_argv)(
                        argv,
                        cwd,
                        self._environment(operation),
                    )
                else:
                    with parent.observer_lock:
                        if cancel_event is not None and cancel_event.is_set():
                            return "cancelled"
                        if (
                            parent.completed.is_set()
                            or parent.stop.is_set()
                            or parent.collection_done.is_set()
                            or parent.proc is None
                            or not self._matches_parent(operation.request, parent)
                        ):
                            return "unknown"
                        proc = (spawn or ShellRunner().spawn_argv)(
                            argv,
                            cwd,
                            self._environment(operation),
                        )
            operation.proc = proc
            if proc.stdout is None or proc.stderr is None:
                return "launch_error"
        except OSError, TypeError, ValueError, RuntimeError:
            return "launch_error"
        try:
            self._write_record(operation, "running")
            if operation.indexed:
                self._set_index(operation)
            self._journal(operation, "running")
            self._put(
                operation, ToolEvent(kind="started", result=_running_result(operation))
            )
            return None
        except OSError:
            return "evidence_error"

    def _start_worker(
        self,
        operation: _Operation,
        cancel_event: threading.Event | _CombinedCancel | None,
    ) -> None:
        operation.worker = threading.Thread(
            target=self._collect, args=(operation, cancel_event)
        )
        operation.worker.start()

    def _collect(
        self,
        operation: _Operation,
        cancel_event: threading.Event | _CombinedCancel | None,
    ) -> None:
        proc = operation.proc
        assert proc is not None and proc.stdout is not None and proc.stderr is not None
        deadline = (
            None
            if operation.request.timeout_seconds is None
            else time.monotonic() + operation.request.timeout_seconds
        )
        watcher = threading.Thread(
            target=self._watch, args=(operation, cancel_event, deadline)
        )
        selector = None
        stdout = bytearray()
        stderr = bytearray()
        failed: str | None = None
        discovery = operation.request.preparation == "discover"
        try:
            selector = selectors.DefaultSelector()
            for pipe, kind in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, kind)
            watcher.start()
            while selector.get_map() or not operation.leader_exited():
                if operation.stop.is_set():
                    failed = operation.reason or "cancelled"
                    break
                for key, _ in selector.select(timeout=0.05):
                    try:
                        chunk = os.read(key.fd, _MAX_CHUNK)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    kind = key.data
                    if discovery:
                        collected = stdout if kind == "stdout" else stderr
                        cap = (
                            operation.request.max_stdout_bytes
                            if kind == "stdout"
                            else operation.request.max_stderr_bytes
                        )
                        if cap is None or len(collected) + len(chunk) > cap:
                            failed = f"{kind}_limit"
                            operation.request_stop(failed)
                            break
                        collected.extend(chunk)
                    self._append_output(operation, kind, chunk)
                    if not discovery:
                        self._put(operation, ToolEvent(kind=kind, data=chunk))
        except OSError:
            failed = "evidence_error"
        except Exception:
            failed = "unknown"
        finally:
            if failed is not None:
                operation.request_stop(failed)
            # The watchdog watches collection, not publication of its result.
            # Signal it before joining; otherwise every normal child deadlocks.
            operation.collection_done.set()
            try:
                operation.terminate()
            except Exception:
                failed = "unknown"
            if selector is not None:
                selector.close()
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except OSError:
                    failed = failed or "evidence_error"
            if watcher.ident is not None:
                watcher.join(timeout=5)
                if watcher.is_alive():
                    failed = "unknown"
        if failed is None and operation.stop.is_set():
            failed = operation.reason or "cancelled"
        if failed is None:
            result = ToolResult(
                status="completed",
                exit_code=proc.returncode,
                owner_ref=operation.owner_ref,
                generation=operation.generation,
                source_revision=operation.context.source_revision,
                stdout=bytes(stdout) if discovery else b"",
                stderr=bytes(stderr) if discovery else b"",
            )
            self._finish(operation, result.status, result)
        else:
            self._finish(operation, failed)

    def _watch(
        self,
        operation: _Operation,
        cancel_event: threading.Event | _CombinedCancel | None,
        deadline: float | None,
    ) -> None:
        while not operation.collection_done.is_set() and not operation.stop.is_set():
            if cancel_event is not None and cancel_event.is_set():
                reason = "cancelled"
            elif deadline is not None and time.monotonic() >= deadline:
                reason = "timeout"
            else:
                try:
                    if operation.indexed and operation.session_context is not None and not operation.ready_announced:
                        operation.check_capability_root()
                        if operation.session_context.readiness_acknowledged():
                            operation.ready_announced = True
                            self._put(operation, ToolEvent("ready", result=_running_result(operation)))
                    if operation.leader_exited():
                        # Retain the waitable leader while stopping descendants.
                        operation.terminate()
                except Exception:
                    operation.request_stop("unknown")
                    return
                operation.collection_done.wait(0.02)
                continue
            operation.request_stop(reason)
            try:
                operation.terminate()
            except Exception:
                operation.request_stop("unknown")
            return

    def _finish(
        self, operation: _Operation, status: str, result: ToolResult | None = None
    ) -> None:
        observer_done = operation.cancel_observers()
        if any(not done.wait(2) for done in observer_done):
            status = "unknown"
        try:
            operation.terminate()  # Also handles a leader that already exited.
        except Exception:
            status = "unknown"
        if operation.indexed and operation.session_context is not None:
            if status == "completed" and not operation.ready_announced:
                status = "launch_error"
            try:
                operation.check_capability_root()
                if not operation.session_context.cleanup_acknowledged():
                    status = "unknown"
                self._remove_socket_directory(operation)
            except (OSError, SessionError):
                status = "unknown"
        try:
            self._discard_inputs(operation)
        except OSError:
            status = "unknown"
            result = None
        if status not in _TERMINAL:
            status = "unknown"
        if result is None or result.status != status:
            result = ToolResult(
                status=status,
                owner_ref=operation.owner_ref,
                generation=operation.generation,
                source_revision=operation.context.source_revision,
            )
        cleanup_known = status != "unknown"
        if operation.publish_result is not None:
            try:
                result = operation.publish_result(result)
                status = result.status
            except Exception:
                status = "evidence_error"
                result = ToolResult(
                    status=status,
                    owner_ref=operation.owner_ref,
                    generation=operation.generation,
                    source_revision=operation.context.source_revision,
                )
        try:
            self._sync_output(operation)
            self._write_record(operation, status, exit_code=result.exit_code)
            if operation.indexed and cleanup_known:
                self._clear_index(operation.context.task, operation)
            self._journal(operation, status)
        except OSError:
            status = "evidence_error" if cleanup_known else "unknown"
            result = ToolResult(
                status=status,
                owner_ref=operation.owner_ref,
                generation=operation.generation,
                source_revision=operation.context.source_revision,
            )
            try:
                self._write_record(operation, status)
                if operation.indexed and cleanup_known:
                    self._clear_index(operation.context.task, operation)
            except OSError:
                pass  # The retained admission record still excludes this task.
        operation.close_context()
        if operation.parent_done is not None and cleanup_known:
            operation.parent_done.set()
        operation.result = result
        operation.completed.set()
        with self._lock:
            if operation.indexed and status != "unknown":
                self._operations.pop((operation.owner_ref, operation.generation), None)

    def _consume(self, operation: _Operation) -> Generator[ToolEvent, None, None]:
        try:
            while True:
                try:
                    event = operation.delivery.get(timeout=0.05)
                except queue.Empty:
                    if operation.completed.is_set():
                        yield _result_event(operation.result)
                        return
                    continue
                yield event
        finally:
            if not operation.completed.is_set():
                operation.request_stop("cancelled")
                try:
                    operation.terminate()
                except Exception:
                    operation.request_stop("unknown")
            worker = operation.worker
            if worker is not None:
                worker.join(timeout=5)
                if worker.is_alive():
                    operation.request_stop("unknown")

    def _put(self, operation: _Operation, event: ToolEvent) -> bool:
        while True:
            try:
                operation.delivery.put(event, timeout=0.05)
                return True
            except queue.Full:
                if operation.stop.is_set():
                    return False

    def _environment(self, operation: _Operation) -> dict[str, str]:
        environment = tool_runtime_environment()
        environment.update(operation.request.env)
        environment.update(self._private_input_environment(operation))
        if operation.session_context is not None:
            request = operation.session_context.request
            if request is not None and request.capture is not None:
                environment.update({
                    "MSHIP_CAPTURE_DIR": str(request.capture.directory),
                    "MSHIP_CAPTURE_KINDS": ",".join(request.capture.kinds),
                    "MSHIP_CAPTURE_PLATFORM": request.capture.platform,
                })
        environment.update(
            {
                "MSHIP_TASK": operation.context.task,
                "MSHIP_REPO": operation.context.repo,
                "MSHIP_SOURCE_REVISION": operation.context.source_revision,
            }
        )
        return environment

    @staticmethod
    def _check_root(operation: _Operation) -> None:
        with operation.cleanup_lock:
            if operation.root_fd is None:
                raise OSError("operation context is closed")
            pinned = os.fstat(operation.root_fd)
            current = operation.root_path.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
                or operation.root_path.resolve(strict=True) != operation.root_path
            ):
                raise OSError("operation context was replaced")

    @contextmanager
    def _pinned_cwd(self, operation: _Operation):
        self._check_root(operation)
        candidate = self._resolve_cwd(operation.root_path, operation.request.cwd)
        candidate_info = candidate.stat(follow_symlinks=False)
        relative = candidate.relative_to(operation.root_path)
        with operation.cleanup_lock:
            if operation.root_fd is None:
                raise OSError("operation context is closed")
            fd = os.dup(operation.root_fd)
        try:
            for part in relative.parts:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
                os.close(fd)
                fd = child
            final = os.fstat(fd)
            if (final.st_dev, final.st_ino) != (
                candidate_info.st_dev,
                candidate_info.st_ino,
            ):
                raise OSError("operation cwd was replaced")
            self._check_root(operation)
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _wrapped_argv(argv: Sequence[str], env_runner: str | None) -> tuple[str, ...]:
        return (
            tuple(argv)
            if env_runner is None
            else ("/bin/sh", "-c", f'{env_runner} "$@"', "mship-tool", *argv)
        )

    @staticmethod
    def _resolve_cwd(worktree: Path, cwd: str) -> Path:
        root = Path(worktree).resolve(strict=True)
        candidate = (root / cwd).resolve(strict=True)
        if not candidate.is_dir() or root not in (candidate, *candidate.parents):
            raise ValueError("cwd is outside verified worktree")
        return candidate

    def _task_dir(self, task: str) -> Path:
        return self._root / hashlib.sha256(task.encode()).hexdigest()

    def _record_path(self, task: str, owner: str) -> Path:
        return self._task_dir(task) / f"{owner}.json"

    @contextmanager
    def _open_storage_dir(
        self, directory: Path, *, create: bool = False, private: bool = True
    ):
        relative = directory.relative_to(self._state_dir)
        if ".." in relative.parts:
            raise OSError("unsafe operation storage")
        # The configured boundary's parent may be a legitimate platform alias
        # (for example /var on macOS). Never follow links at or below it.
        boundary = self._state_dir.parent.resolve() / self._state_dir.name
        target = boundary / relative
        fd = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for index, part in enumerate(target.parts[1:], start=1):
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    else:
                        os.fsync(fd)
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
                try:
                    info = os.fstat(child)
                    if index >= len(boundary.parts) - 1 and info.st_uid != os.getuid():
                        raise OSError("unsafe operation storage owner")
                    if (
                        private
                        and index >= len(boundary.parts)
                        and info.st_mode & 0o077
                    ):
                        raise OSError("operation storage is not private")
                except BaseException:
                    os.close(child)
                    raise
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _operation_directory(self, operation: _Operation):
        if operation.storage_fd is None:
            raise OSError("operation storage is closed")
        with self._open_storage_dir(operation.record_path.parent) as current_fd:
            current, pinned = os.fstat(current_fd), os.fstat(operation.storage_fd)
            if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise OSError("operation storage was replaced")
            yield operation.storage_fd

    @staticmethod
    def _check_private_file(fd: int) -> os.stat_result:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise OSError("unsafe private operation file")
        return info

    def _private_input_environment(self, operation: _Operation) -> dict[str, str]:
        if not operation.private_inputs:
            return {}
        with self._operation_directory(operation) as directory_fd:
            for private_input in operation.private_inputs.values():
                fd = os.open(
                    private_input.name,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    info = self._check_private_file(fd)
                    if (info.st_dev, info.st_ino) != (
                        private_input.device,
                        private_input.inode,
                    ):
                        raise OSError("private operation input was replaced")
                finally:
                    os.close(fd)
        return {
            name: str(operation.record_path.parent / private_input.name)
            for name, private_input in operation.private_inputs.items()
        }

    def _prepare_inputs(self, operation: _Operation) -> None:
        inputs = dict(operation.request.input_files)
        if operation.session_context is not None:
            inputs[OWNER_CONTEXT_FILE] = json.dumps(
                operation.session_context.to_private_dict(), separators=(",", ":")
            )
        if not inputs:
            return
        try:
            with self._operation_directory(operation) as directory_fd:
                for environment_name, content in inputs.items():
                    name = f"{operation.record_path.stem}.input-{secrets.token_hex(16)}"
                    fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    try:
                        self._full_write(fd, content.encode("utf-8"))
                        os.fsync(fd)
                        info = self._check_private_file(fd)
                        private_input = _PrivateInput(
                            name=name,
                            device=info.st_dev,
                            inode=info.st_ino,
                        )
                        operation.private_inputs[environment_name] = private_input
                    finally:
                        os.close(fd)
                os.fsync(directory_fd)
        except BaseException:
            try:
                self._discard_inputs(operation)
            except OSError:
                pass
            raise

    def _discard_inputs(self, operation: _Operation) -> None:
        if not operation.private_inputs:
            return
        with self._operation_directory(operation) as directory_fd:
            for environment_name, private_input in tuple(
                operation.private_inputs.items()
            ):
                fd = os.open(
                    private_input.name,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    info = self._check_private_file(fd)
                    if (info.st_dev, info.st_ino) != (
                        private_input.device,
                        private_input.inode,
                    ):
                        raise OSError("private operation input was replaced")
                finally:
                    os.close(fd)
                os.unlink(private_input.name, dir_fd=directory_fd)
                operation.private_inputs.pop(environment_name)
            os.fsync(directory_fd)

    def _prepare_output(self, operation: _Operation) -> None:
        with self._operation_directory(operation) as directory_fd:
            for kind, path in (
                ("stdout", operation.stdout_path),
                ("stderr", operation.stderr_path),
            ):
                fd = os.open(
                    path.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                operation.output_fds[kind] = fd
                self._check_private_file(fd)

    @staticmethod
    def _full_write(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short operation write")
            view = view[count:]

    def _append_output(self, operation: _Operation, kind: str, data: bytes) -> None:
        self._full_write(operation.output_fds[kind], data)

    def _sync_output(self, operation: _Operation) -> None:
        for fd in operation.output_fds.values():
            self._check_private_file(fd)
            os.fsync(fd)

    def _publish_json(self, directory_fd: int, name: str, payload: bytes) -> None:
        temporary = f".{name}.{secrets.token_hex(8)}"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            try:
                self._full_write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(
                temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
            )
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def _read_json(self, directory_fd: int, name: str, limit: int) -> dict[str, Any]:
        fd = os.open(
            name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd
        )
        try:
            if self._check_private_file(fd).st_size > limit:
                raise OSError("operation metadata exceeds limit")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(limit + 1)
            if len(raw) > limit:
                raise OSError("operation metadata exceeds limit")
            decoded = json.loads(raw.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise OSError("invalid operation metadata")
            return decoded
        except (ValueError, RecursionError) as exc:
            raise OSError("invalid operation metadata") from exc
        finally:
            os.close(fd)

    def _write_record(
        self,
        operation: _Operation,
        status: str,
        *,
        exit_code: int | None = None,
    ) -> None:
        payload = json.dumps(
            {
                "version": 1,
                "owner_ref": operation.owner_ref,
                "generation": operation.generation,
                "task": operation.context.task,
                "repo": operation.context.repo,
                "source_revision": operation.context.source_revision,
                "worktree": str(operation.root_path),
                "status": status,
                "exit_code": exit_code,
                "session_private_root": (
                    None if operation.session_context is None
                    else str(operation.session_context.private_root)
                ),
            },
            separators=(",", ":"),
        ).encode()
        with self._operation_directory(operation) as directory_fd:
            self._publish_json(directory_fd, operation.record_path.name, payload)

    def _active_path(self, task: str) -> Path:
        return self._task_dir(task) / "active.json"

    def _read_active(
        self, task: str, *, directory_fd: int | None = None
    ) -> dict[str, str] | None:
        try:
            if directory_fd is None:
                with self._open_storage_dir(self._task_dir(task)) as opened_fd:
                    return self._read_active(task, directory_fd=opened_fd)
            data = self._read_json(directory_fd, "active.json", _INDEX_LIMIT)
        except FileNotFoundError:
            return None
        if (
            set(data) != {"owner_ref", "generation"}
            or not _safe_identifier(data.get("owner_ref"))
            or not _safe_identifier(data.get("generation"))
        ):
            raise OSError("corrupt active index")
        return data

    def _set_index(self, operation: _Operation) -> None:
        payload = json.dumps(
            {"owner_ref": operation.owner_ref, "generation": operation.generation},
            separators=(",", ":"),
        ).encode()
        with self._operation_directory(operation) as directory_fd:
            self._publish_json(directory_fd, "active.json", payload)

    def _clear_index(self, task: str, operation: _Operation) -> None:
        with self._operation_directory(operation) as directory_fd:
            active = self._read_active(task, directory_fd=directory_fd)
            if active is None:
                return
            if active != {
                "owner_ref": operation.owner_ref,
                "generation": operation.generation,
            }:
                raise OSError("active index belongs to another operation")
            os.unlink("active.json", dir_fd=directory_fd)
            os.fsync(directory_fd)

    def _journal(self, operation: _Operation, status: str) -> None:
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            message=status,
            repo=operation.context.repo,
            action="remote-tool-operation",
            id=operation.owner_ref,
            parent=operation.generation,
        )
        with self._open_storage_dir(
            self._state_dir / "logs", create=True, private=False
        ) as directory_fd:
            name = f"{operation.context.task}.md"
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o002
                ):
                    raise OSError("unsafe operation journal")
                prefix = (
                    f"# Task Log: {operation.context.task}\n"
                    if info.st_size == 0
                    else ""
                )
                self._full_write(fd, (prefix + format_log_entry(entry)).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory_fd)

    def _read_record(self, task: str, owner: str) -> dict[str, Any] | None:
        if not _safe_identifier(owner):
            return None
        try:
            with self._open_storage_dir(self._task_dir(task)) as directory_fd:
                return self._read_json(directory_fd, f"{owner}.json", _METADATA_LIMIT)
        except OSError:
            return None


@dataclass(frozen=True)
class _CombinedCancel:
    external: threading.Event
    internal: threading.Event

    def is_set(self) -> bool:
        return self.external.is_set() or self.internal.is_set()


def _combined_cancel(
    external: threading.Event | None,
    internal: threading.Event,
) -> threading.Event | _CombinedCancel:
    return internal if external is None else _CombinedCancel(external, internal)


def _running_result(operation: _Operation) -> ToolResult:
    return ToolResult(
        status="running",
        owner_ref=operation.owner_ref,
        generation=operation.generation,
        source_revision=operation.context.source_revision,
    )


def _result_event(result: ToolResult) -> ToolEvent:
    return ToolEvent(kind="result", result=result)


def _safe_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value.isascii()
        and all(c.isalnum() or c in "-_" for c in value)
    )


def _exit_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
