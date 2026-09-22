"""A framework-only lifetime owner for one Flutter ``run --machine`` child."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from mship.core.flutter_adapter import (
    FlutterLaunchConfig,
    _tool_environment,
    attest_ios_foreground,
    capture_ios,
    discovery_candidate,
    load_discovery_config,
    load_launch_config,
    redact_log,
    reported_flutter_capabilities,
    revalidate_target,
)
from mship.core.session_channel import (
    OwnerClient,
    OwnerContext,
    acquire_app_lease,
    read_private_json,
    serve_owner,
)
from mship.core.session_inputs import (
    CaptureGrant,
    OwnerRequest,
    SessionError,
    identifier,
    source_revision,
    strict_object,
)

_MACHINE_LINE_LIMIT = 128 * 1024
_MACHINE_START_TIMEOUT = 45.0
_MACHINE_COMMAND_TIMEOUT = 20.0
_CAPTURE_RECEIPT_LIMIT = 4096


def _session_error(code: str, message: str) -> SessionError:
    return SessionError(code, message)


def _safe_result(state: str, *, mode: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"state": state, "provenance": "unknown"}
    if mode is not None:
        result["mode"] = mode
    return result


@dataclass
class _PendingUpdate:
    update_id: str
    old_source_revision: str
    new_source_revision: str
    committed: bool = False
    committing: bool = False
    released: bool = False


class FlutterSessionOwner:
    """Own a single exact Flutter machine process, never an ambient daemon.

    The owner is initialized only from three server-created private files.  Public
    owner requests carry no device or app selector; all operations revalidate the
    recorded binding before using the already-recorded private app identity.
    """

    def __init__(self, config: FlutterLaunchConfig, *, worktree: Path, source: str):
        if not worktree.is_absolute() or not worktree.is_dir():
            raise _session_error(
                "unavailable", "Pinned Flutter worktree is unavailable"
            )
        source_revision(source)
        entrypoint = (worktree / config.options.entrypoint).resolve(strict=False)
        if (
            not entrypoint.is_relative_to(worktree.resolve())
            or not entrypoint.is_file()
        ):
            raise _session_error(
                "unavailable", "Reviewed Flutter entrypoint is unavailable"
            )
        self.config = config
        self.worktree = worktree.resolve()
        self._source = source
        self._state_lock = threading.RLock()
        self._machine_lock = threading.Lock()
        self._update_lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._app_lease: BinaryIO | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._app_id: str | None = None
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._child_exited = threading.Event()
        self._unhealthy = False
        self._cleaned = False
        self._command_number = 0
        self._pending_update: _PendingUpdate | None = None
        self._lifetime_stop: threading.Event | None = None
        self._responses: dict[str, queue.Queue[dict[str, object]]] = {}
        self._subscribers: set[queue.Queue[dict[str, object]]] = set()
        self._handlers: dict[str, threading.Event] = {}
        self._handler_condition = threading.Condition(self._state_lock)

    @property
    def current_source_revision(self) -> str:
        with self._state_lock:
            return self._source

    def _broadcast(self, event: dict[str, object]) -> None:
        with self._state_lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # A slow observer loses only its own log events.
                continue

    def bind_lifetime_stop(self, stop: threading.Event) -> None:
        self._lifetime_stop = stop

    def _status(self, state: str) -> dict[str, object]:
        fingerprint = hashlib.sha256(
            self.config.target.target_fingerprint.encode()
        ).hexdigest()
        capabilities = ["logs"]
        if self.config.target.platform == "android" or (
            self.config.target.ios is not None
            and self.config.target.ios.supports_capture
        ):
            capabilities.append("capture")
        return {
            "version": 1,
            "run_id": self.config.context["run_id"],
            "state": state,
            "source_revision": self.current_source_revision,
            "platform": self.config.target.platform,
            "transport": self.config.target.transport,
            "target_fingerprint": fingerprint,
            "binary_provenance": {"known": False},
            "capabilities": capabilities,
            "resources": {"framework_child": "owned"},
        }

    def _set_unhealthy(self) -> None:
        with self._state_lock:
            self._unhealthy = True
            self._started.set()
        self._broadcast({"terminal": True})

    def _machine_ready(self) -> str:
        with self._state_lock:
            if self._unhealthy or self._cleaned:
                raise _session_error(
                    "unknown", "Flutter framework session health is unknown"
                )
            if self._proc is None or self._proc.poll() is not None:
                self._unhealthy = True
                raise _session_error(
                    "unknown", "Flutter framework child is unavailable"
                )
            if self._app_id is None or not self._started.is_set():
                raise _session_error(
                    "unavailable", "Flutter framework app is not ready"
                )
            return self._app_id

    def _revalidate_live(self, *, hot: str | None = None) -> str:
        app_id = self._machine_ready()
        if self.config.options.mode != "debug" and hot is not None:
            raise _session_error(
                "unavailable", "Flutter hot operations require debug mode"
            )
        try:
            revalidate_target(self.config.target)
            if hot is not None:
                reported_reload, reported_restart = reported_flutter_capabilities(
                    self.config.executable, self.config.target
                )
            else:
                reported_reload, reported_restart = False, False
        except SessionError:
            self._set_unhealthy()
            raise
        if hot == "reload" and not (self.config.target.hot_reload and reported_reload):
            raise _session_error(
                "unavailable", "Flutter hot reload capability is unavailable"
            )
        if hot == "restart" and not (
            self.config.target.hot_restart and reported_restart
        ):
            raise _session_error(
                "unavailable", "Flutter hot restart capability is unavailable"
            )
        return app_id

    def start(self, cancel: threading.Event | None = None) -> None:
        """Start only the selected target and wait for matching app.start/app.started."""
        with self._state_lock:
            if self._proc is not None:
                raise _session_error(
                    "invalid", "Flutter framework owner is already started"
                )
        if cancel is not None and cancel.is_set():
            raise _session_error("cancelled", "Flutter session launch was cancelled")
        try:
            revalidate_target(self.config.target)
        except SessionError:
            self._set_unhealthy()
            raise
        with self._state_lock:
            if self._proc is not None or self._app_lease is not None or self._cleaned:
                raise _session_error(
                    "invalid", "Flutter framework owner is already started"
                )
            target = self.config.target
            device_id = (
                target.android.serial
                if target.android is not None
                else target.device_id
            )
            self._app_lease = acquire_app_lease(
                target.platform, device_id, target.app_id
            )
        environment = _tool_environment()
        try:
            proc = subprocess.Popen(
                self.config.argv(),
                cwd=self.worktree,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            self._release_app()
            raise _session_error(
                "unavailable", "Flutter tool is unavailable on the selected host"
            ) from exc
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:
            proc.terminate()
            proc.wait(timeout=1)
            raise _session_error("unknown", "Flutter machine transport is unavailable")
        with self._state_lock:
            self._proc = proc
        self._reader = threading.Thread(
            target=self._read_machine, args=(proc,), daemon=True
        )
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, args=(proc,), daemon=True
        )
        self._reader.start()
        self._stderr_reader.start()
        deadline = time.monotonic() + _MACHINE_START_TIMEOUT
        while not self._started.wait(0.05):
            if cancel is not None and cancel.is_set():
                self.cleanup()
                raise _session_error(
                    "cancelled", "Flutter session launch was cancelled"
                )
            if proc.poll() is not None or time.monotonic() >= deadline:
                self._set_unhealthy()
                self.cleanup()
                raise _session_error(
                    "unknown", "Flutter framework readiness was not acknowledged"
                )
        if self._unhealthy or self._app_id is None:
            self.cleanup()
            raise _session_error(
                "unknown", "Flutter framework readiness was not acknowledged"
            )

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        # Tool diagnostics can contain device paths and control addresses.  Consume
        # them to avoid deadlock, but never forward or persist them outside #507.
        assert proc.stderr is not None
        try:
            while proc.stderr.read(4096):
                pass
        finally:
            self._child_exited.set()

    def _read_machine(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        pending = bytearray()
        valid = True
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                pending.extend(chunk)
                while (end := pending.find(b"\n")) >= 0:
                    if end > _MACHINE_LINE_LIMIT:
                        valid = False
                        break
                    line = bytes(pending[:end]).strip()
                    del pending[: end + 1]
                    if not line or not line.startswith(b"["):
                        continue
                    try:
                        records = json.loads(
                            line.decode("utf-8"), object_pairs_hook=_no_duplicates
                        )
                    except UnicodeError, ValueError, json.JSONDecodeError:
                        valid = False
                        break
                    if (
                        not isinstance(records, list)
                        or not records
                        or not all(isinstance(item, dict) for item in records)
                    ):
                        valid = False
                        break
                    if any(not self._record_machine(record) for record in records):
                        valid = False
                        break
                if not valid or len(pending) > _MACHINE_LINE_LIMIT:
                    valid = valid and len(pending) <= _MACHINE_LINE_LIMIT
                    break
            if pending:
                valid = False
        finally:
            self._child_exited.set()
            if not valid or not self._stopped.is_set():
                self._set_unhealthy()
            if not self._cleaned and self._lifetime_stop is not None:
                self._lifetime_stop.set()

    def _record_machine(self, record: dict[str, object]) -> bool:
        if "id" in record and "result" in record and set(record) == {"id", "result"}:
            request_id = record["id"]
            if not isinstance(request_id, str) or not isinstance(
                record["result"], (dict, bool)
            ):
                return False
            with self._machine_lock:
                recipient = self._responses.get(request_id)
            if recipient is None:
                return False
            try:
                result = record["result"]
                recipient.put_nowait(
                    dict(result) if isinstance(result, dict) else {"result": result}
                )
            except queue.Full:
                return False
            return True
        if (
            set(record) != {"event", "params"}
            or not isinstance(record["event"], str)
            or not isinstance(record["params"], dict)
        ):
            return False
        event, params = record["event"], record["params"]
        if event == "app.start":
            return self._on_start(params)
        if event == "app.started":
            return self._on_started(params)
        if event == "app.stop":
            return self._on_stop(params)
        if event == "app.log":
            return self._on_log(params)
        return True

    def _on_start(self, params: dict[str, object]) -> bool:
        needed = {"appId", "directory", "deviceId", "launchMode", "mode"}
        if not needed.issubset(params) or any(
            not isinstance(params[key], str) for key in needed
        ):
            return False
        app_id = params["appId"]
        directory = params["directory"]
        if (
            not app_id
            or len(app_id) > 1024
            or any(ord(char) < 32 for char in app_id)
            or params["deviceId"] != self.config.target.device_id
            or params["launchMode"] != "run"
            or params["mode"] != self.config.options.mode
            or Path(directory).resolve(strict=False) != self.worktree
        ):
            return False
        with self._state_lock:
            if self._app_id is not None and self._app_id != app_id:
                return False
            self._app_id = app_id
        return True

    def _on_started(self, params: dict[str, object]) -> bool:
        if set(params) != {"appId"} or params.get("appId") != self._app_id:
            return False
        self._started.set()
        return True

    def _on_stop(self, params: dict[str, object]) -> bool:
        if set(params) != {"appId"} or params.get("appId") != self._app_id:
            return False
        self._stopped.set()
        self._broadcast({"terminal": True})
        if not self._cleaned and self._lifetime_stop is not None:
            self._lifetime_stop.set()
        return True

    def _on_log(self, params: dict[str, object]) -> bool:
        if (
            set(params) - {"appId", "log", "error"}
            or params.get("appId") != self._app_id
            or not isinstance(params.get("log"), str)
            or ("error" in params and type(params["error"]) is not bool)
        ):
            return False
        self._broadcast(
            {
                "message": redact_log(params["log"]),
                "error": bool(params.get("error", False)),
            }
        )
        return True

    def _request_restart(
        self, *, full_restart: bool, cancel: threading.Event | None = None
    ) -> None:
        app_id = self._revalidate_live(hot="restart" if full_restart else "reload")
        with self._machine_lock:
            self._command_number += 1
            request_id = f"restart-{self._command_number}"
            recipient: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
            self._responses[request_id] = recipient
            proc = self._proc
            if proc is None or proc.stdin is None:
                del self._responses[request_id]
                raise _session_error(
                    "unknown", "Flutter machine transport is unavailable"
                )
            frame = (
                json.dumps(
                    [
                        {
                            "method": "app.restart",
                            "id": request_id,
                            "params": {"appId": app_id, "fullRestart": full_restart},
                        }
                    ],
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            try:
                proc.stdin.write(frame)
                proc.stdin.flush()
            except OSError as exc:
                del self._responses[request_id]
                self._set_unhealthy()
                raise _session_error(
                    "unknown", "Flutter machine transport was lost"
                ) from exc
        deadline = time.monotonic() + _MACHINE_COMMAND_TIMEOUT
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise _session_error(
                        "cancelled", "Flutter hot operation was cancelled"
                    )
                if self._unhealthy or self._cleaned:
                    raise _session_error(
                        "unknown", "Flutter framework session health is unknown"
                    )
                try:
                    response = recipient.get(timeout=0.05)
                except queue.Empty:
                    if time.monotonic() >= deadline:
                        self._set_unhealthy()
                        raise _session_error(
                            "unknown", "Flutter restart acknowledgement was lost"
                        )
                    continue
                if (
                    set(response) != {"code", "message"}
                    or type(response["code"]) is not int
                    or not isinstance(response["message"], str)
                ):
                    self._set_unhealthy()
                    raise _session_error(
                        "unknown", "Flutter restart response is invalid"
                    )
                if response["code"] != 0:
                    raise _session_error("unavailable", "Flutter restart was rejected")
                return
        finally:
            with self._machine_lock:
                self._responses.pop(request_id, None)

    def handle(
        self,
        request: OwnerRequest,
        payload: dict[str, object],
        cancel: threading.Event,
        emit: Callable[[dict[str, object]], None],
    ) -> dict[str, object]:
        """Admit only one exact claim; reserve source changes against active handlers."""
        if not isinstance(request, OwnerRequest) or not isinstance(payload, dict):
            raise _session_error("invalid", "Invalid Flutter owner request")
        if request.operation == "cleanup":
            strict_object(payload, set())
            if not self.cleanup():
                raise _session_error(
                    "unknown", "Flutter cleanup acknowledgement is incomplete"
                )
            return self._status("stopped")
        if request.source_revision != self.current_source_revision:
            raise _session_error(
                "invalid", "Flutter owner source authorization is stale"
            )
        if request.operation in {"source-reserve", "source-commit"}:
            data = strict_object(payload, {"update_id", "new_source_revision"})
            update_id = identifier(data["update_id"])
            revision = source_revision(data["new_source_revision"])
            if request.operation == "source-reserve":
                return self.reserve_source_update(update_id, revision)
            return self.commit_source_update(update_id, revision)
        if request.operation in {"source-release", "source-abort"}:
            data = strict_object(payload, {"update_id"})
            update_id = identifier(data["update_id"])
            if request.operation == "source-release":
                return self.release_source_update(update_id, cancel)
            return self.abort_source_update(update_id)
        with self._handler_condition:
            if self._pending_update is not None:
                raise _session_error(
                    "busy", "Flutter source update excludes observer operations"
                )
            if self._cleaned or request.source_revision != self._source:
                raise _session_error(
                    "unknown", "Flutter observation identity is no longer live"
                )
            if request.operation_ref in self._handlers:
                raise _session_error(
                    "invalid", "Flutter observation is already admitted"
                )
            self._handlers[request.operation_ref] = cancel
        try:
            if request.operation == "capture":
                return self._capture(request, payload, cancel)
            strict_object(payload, set())
            if request.operation == "status":
                self._revalidate_live()
            elif request.operation == "logs":
                return self._logs(cancel, emit)
            elif request.operation in {"reload", "restart"}:
                self._request_restart(
                    full_restart=request.operation == "restart", cancel=cancel
                )
            else:
                raise _session_error("invalid", "Unsupported Flutter owner operation")
            return self._status("active")
        finally:
            with self._handler_condition:
                self._handlers.pop(request.operation_ref, None)
                self._handler_condition.notify_all()

    def _drain_handlers(self) -> None:
        deadline = time.monotonic() + 10
        with self._handler_condition:
            for cancel in self._handlers.values():
                cancel.set()
            while self._handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _session_error(
                        "unknown", "Flutter observer cleanup is incomplete"
                    )
                self._handler_condition.wait(timeout=remaining)

    def _logs(
        self, cancel: threading.Event, emit: Callable[[dict[str, object]], None]
    ) -> dict[str, object]:
        self._revalidate_live()
        subscriber: queue.Queue[dict[str, object]] = queue.Queue(maxsize=128)
        with self._state_lock:
            self._subscribers.add(subscriber)
        try:
            while not cancel.is_set():
                try:
                    event = subscriber.get(timeout=0.1)
                except queue.Empty:
                    self._machine_ready()
                    continue
                if event.get("terminal") is True:
                    raise _session_error(
                        "unknown", "Flutter framework session ended during logs"
                    )
                emit(
                    {
                        "kind": "app-log",
                        "message": event["message"],
                        "error": event["error"],
                    }
                )
            raise _session_error("cancelled", "Flutter log subscription was cancelled")
        finally:
            with self._state_lock:
                self._subscribers.discard(subscriber)

    def _capture(
        self, request: OwnerRequest, payload: dict[str, object], cancel: threading.Event
    ) -> dict[str, object]:
        if payload or request.capture is None:
            raise _session_error("invalid", "Invalid Flutter capture request")
        if self.config.target.platform == "ios" and (
            self.config.target.ios is None
            or not self.config.target.ios.supports_capture
        ):
            raise _session_error(
                "unavailable", "Selected Flutter iOS target cannot attest capture"
            )
        grant = request.capture
        if grant.platform != self.config.target.platform:
            raise _session_error(
                "invalid", "Capture platform does not match Flutter target"
            )
        if cancel.is_set():
            raise _session_error("cancelled", "Flutter capture was cancelled")
        machine_app_id = self._revalidate_live()
        self._foreground_attest(machine_app_id, cancel)
        if cancel.is_set():
            raise _session_error("cancelled", "Flutter capture was cancelled")
        if self.config.target.platform == "android":
            target = self.config.target.android
            assert target is not None
            from mship.core.android_adapter import capture_android

            capture_android(
                target.adb, target.serial, grant.directory, grant.kinds, cancel
            )
        else:
            capture_ios(self.config.target, grant.directory, grant.kinds, cancel)
        if cancel.is_set():
            raise _session_error("cancelled", "Flutter capture was cancelled")
        self._foreground_attest(machine_app_id, cancel)
        self._write_capture_receipt(grant, request.operation_ref)
        return self._status("active")

    def _foreground_attest(self, machine_app_id: str, cancel: threading.Event) -> None:
        try:
            revalidate_target(self.config.target, cancel)
        except SessionError:
            self._set_unhealthy()
            raise
        if machine_app_id != self._machine_ready():
            self._set_unhealthy()
            raise _session_error("unknown", "Flutter machine app identity is stale")
        if self.config.target.platform == "android":
            target = self.config.target.android
            assert target is not None
            from mship.core.android_adapter import foreground_android

            foreground = foreground_android(target.adb, target.serial)
            if foreground != self.config.target.app_id:
                raise _session_error(
                    "unavailable", "Selected Flutter app is not foreground"
                )
            return
        attest_ios_foreground(self.config.target, cancel)

    def _write_capture_receipt(self, grant: CaptureGrant, operation_ref: str) -> None:
        self._machine_ready()
        receipt = {
            "version": 1,
            "operation_ref": operation_ref,
            "platform": self.config.target.platform,
            "transport": self.config.target.transport,
            "target_fingerprint": self.config.target.target_fingerprint,
            "app_identity": self.config.target.app_id,
            "foreground_before": True,
            "foreground_after": True,
        }
        raw = json.dumps(receipt, separators=(",", ":")).encode("utf-8")
        if len(raw) > _CAPTURE_RECEIPT_LIMIT:
            raise _session_error("unknown", "Flutter capture receipt is unavailable")
        try:
            directory_fd = os.open(
                grant.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                info = os.fstat(directory_fd)
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise _session_error(
                        "unknown", "Flutter capture receipt is unavailable"
                    )
                descriptor = os.open(
                    "flutter-capture-receipt.json",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    os.write(descriptor, raw)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                os.close(directory_fd)
        except (OSError, SessionError) as exc:
            if isinstance(exc, SessionError):
                raise
            raise _session_error(
                "unknown", "Flutter capture receipt is unavailable"
            ) from exc

    def reserve_source_update(
        self, update_id: str, new_source_revision: str
    ) -> dict[str, object]:
        """Freeze admission and drain existing parent-owned helpers before mutation."""
        identifier(update_id)
        source_revision(new_source_revision)
        if not self._update_lock.acquire(blocking=False):
            raise _session_error("busy", "Flutter source update is already reserved")
        try:
            with self._state_lock:
                if self._pending_update is not None or self._cleaned:
                    raise _session_error("busy", "Flutter source update is unavailable")
                self._pending_update = _PendingUpdate(
                    update_id, self._source, new_source_revision
                )
            self._drain_handlers()
            self._revalidate_live(hot="reload")
            return {"update_id": update_id, "stage": "reserved"}
        except BaseException:
            with self._state_lock:
                self._pending_update = None
            self._update_lock.release()
            raise

    def commit_source_update(
        self, update_id: str, new_source_revision: str
    ) -> dict[str, object]:
        identifier(update_id)
        source_revision(new_source_revision)
        with self._state_lock:
            pending = self._pending_update
            if (
                pending is None
                or pending.update_id != update_id
                or pending.new_source_revision != new_source_revision
                or pending.committed
                or pending.committing
                or self._cleaned
            ):
                raise _session_error(
                    "invalid", "Flutter source commit does not match reservation"
                )
            pending.committing = True
        self._revalidate_live(hot="reload")
        with self._state_lock:
            if self._cleaned:
                raise _session_error(
                    "unknown", "Flutter source owner stopped during commit"
                )
            self._source = new_source_revision
            pending.committed = True
            pending.committing = False
        return {"update_id": update_id, "stage": "context-committed"}

    def release_source_update(
        self, update_id: str, cancel: threading.Event
    ) -> dict[str, object]:
        identifier(update_id)
        with self._state_lock:
            pending = self._pending_update
            if (
                pending is None
                or pending.update_id != update_id
                or pending.released
                or not pending.committed
                or self._source != pending.new_source_revision
            ):
                raise _session_error(
                    "invalid", "Flutter source release does not match committed context"
                )
            # Claim once before sending; cancellation/lost acknowledgement never permits replay.
            pending.released = True
        try:
            if cancel.is_set():
                raise _session_error("cancelled", "Flutter source update was cancelled")
            self._request_restart(full_restart=False, cancel=cancel)
            return {"update_id": update_id, "stage": "reloaded"}
        finally:
            with self._state_lock:
                self._pending_update = None
            self._update_lock.release()

    def abort_source_update(self, update_id: str) -> dict[str, object]:
        identifier(update_id)
        with self._state_lock:
            pending = self._pending_update
            if (
                pending is None
                or pending.update_id != update_id
                or pending.committed
                or pending.committing
                or pending.released
            ):
                raise _session_error(
                    "invalid", "Flutter source reservation cannot be aborted"
                )
            self._pending_update = None
        self._update_lock.release()
        return {"update_id": update_id, "stage": "aborted"}

    def _release_app(self) -> None:
        with self._state_lock:
            if self._app_lease is not None:
                self._app_lease.close()
                self._app_lease = None

    def cleanup(self) -> bool:
        """Stop and reap this child only; unknown acknowledgement stays visible."""
        with self._state_lock:
            if self._cleaned:
                return not self._unhealthy
            self._cleaned = True
            proc = self._proc
            app_id = self._app_id
        try:
            self._drain_handlers()
        except SessionError:
            self._set_unhealthy()
        acknowledged = self._stopped.is_set()
        if proc is None:
            self._release_app()
            return True
        if (
            not acknowledged
            and app_id is not None
            and proc.poll() is None
            and not self._unhealthy
        ):
            try:
                with self._machine_lock:
                    self._command_number += 1
                    request_id = f"stop-{self._command_number}"
                    recipient: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
                    self._responses[request_id] = recipient
                    assert proc.stdin is not None
                    proc.stdin.write(
                        json.dumps(
                            [
                                {
                                    "method": "app.stop",
                                    "id": request_id,
                                    "params": {"appId": app_id},
                                }
                            ],
                            separators=(",", ":"),
                        ).encode()
                        + b"\n"
                    )
                    proc.stdin.flush()
                response = recipient.get(timeout=_MACHINE_COMMAND_TIMEOUT)
                acknowledged = (
                    set(response) == {"result"} and response["result"] is True
                )
                if acknowledged:
                    acknowledged = self._stopped.wait(_MACHINE_COMMAND_TIMEOUT)
            except OSError, queue.Empty, AssertionError:
                acknowledged = False
            finally:
                with self._machine_lock:
                    self._responses.pop(request_id, None)
        if proc.poll() is None:
            try:
                proc.terminate()  # inherited #507 group: signal only our exact child.
                proc.wait(timeout=5)
            except OSError, subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except OSError, subprocess.TimeoutExpired:
                    self._set_unhealthy()
                    return False
        if not acknowledged:
            self._set_unhealthy()
        self._release_app()
        return acknowledged and proc.returncode is not None


def _no_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate protocol member")
        value[key] = item
    return value


def _operation_payload() -> dict[str, object]:
    raw = os.environ.get("MSHIP_SESSION_OPERATION_FILE")
    if raw is None:
        return {}
    try:
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError
        value = read_private_json(path)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, SessionError, ValueError) as exc:
        raise _session_error("invalid", "Invalid Flutter operation payload") from exc


def discover() -> int:
    """Run the configured bounded target discovery task without an owner context."""
    try:
        request, options, executable, rank_schema, targets = load_discovery_config()
        alias = request["target_alias"]
        candidates = [
            discovery_candidate(target, options, executable)
            for target in targets
            if alias is None or alias in target.aliases
        ]
        print(
            json.dumps(
                {
                    "protocol_version": 1,
                    "backend": request["backend"],
                    "backend_revision": request["backend_revision"],
                    "rank_schema": list(rank_schema),
                    "candidates": candidates,
                    "errors": [],
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 0
    except SessionError:
        print(
            json.dumps(
                {
                    "protocol_version": 1,
                    "backend": "invalid",
                    "backend_revision": "invalid",
                    "rank_schema": [],
                    "candidates": [],
                    "errors": [
                        {
                            "code": "flutter_unavailable",
                            "message": "Flutter target discovery failed",
                            "remediation": None,
                        }
                    ],
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 1


def main() -> int:
    """Configured #507 adapter entry point; all stdout stays a safe projection."""
    if len(sys.argv) != 1:
        raise _session_error(
            "invalid", "Flutter adapter accepts no operation arguments"
        )
    context: OwnerContext | None = None
    owner: FlutterSessionOwner | None = None
    cleanup_known = False
    stop = threading.Event()

    def request_stop(_signal: int, _frame: object) -> None:
        stop.set()

    previous_handlers = {
        value: signal.signal(value, request_stop)
        for value in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        context = OwnerContext.from_environ()
        if context.secret is None:
            assert context.request is not None

            def emit(event: dict[str, object]) -> None:
                print(json.dumps(event, separators=(",", ":")), flush=True)

            result = OwnerClient(context).call(
                context.request.operation, _operation_payload(), event_sink=emit
            )
            print(json.dumps(result, separators=(",", ":")), flush=True)
            return 0
        config = load_launch_config()
        owner = FlutterSessionOwner(
            config, worktree=context.worktree, source=context.source_revision
        )
        owner.bind_lifetime_stop(stop)
        context.begin()
        owner.start(stop)
        print(json.dumps(owner._status("active"), separators=(",", ":")), flush=True)
        serve_owner(
            context,
            owner.handle,
            stop,
            source_revision=lambda: owner.current_source_revision,
        )
        cleanup_known = owner.cleanup()
        return 0 if cleanup_known else 1
    except SessionError as error:
        print(
            json.dumps(
                {"state": error.code, "provenance": "unknown"}, separators=(",", ":")
            ),
            flush=True,
        )
        return 1
    finally:
        if owner is not None and not owner._cleaned:
            cleanup_known = owner.cleanup()
        if context is not None and context.secret is not None:
            context.finish(cleanup_known=cleanup_known)
        for value, previous in previous_handlers.items():
            signal.signal(value, previous)


if __name__ == "__main__":
    if sys.argv[1:] == ["discover"]:
        raise SystemExit(discover())
    raise SystemExit(main())
