"""Explicit staged Flutter source updates for one recorded app run.

Only a typed request bound to an existing owner may reserve its source lease.
The server applies an already transferred immutable ref through #507, then
coordinates owner/AppRun acknowledgement before one reload. No setup or build.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any, Literal

from mship.core.remote_tool import ToolProtocolError, ToolRequest
from mship.core.session_inputs import SessionError
from mship.core.session_channel import (
    _private_directory,
    _write_receipt,
    decode_private_json,
    read_private_json,
)
from mship.core.run_target.models import DiscoveryRequest, profile_revision
from mship.core.run_target.models import AppRun, JsonValue

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None

if TYPE_CHECKING:
    from mship.core.persistence.workspace_store import WorkspaceStore
    from mship.core.remote_exec import RemoteExecDeps

_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_PROFILE_REVISION = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_STAGES = frozenset(("prepare", "commit", "release", "abort"))
_REPLY_STAGES = frozenset(
    (
        "prepared",
        "context-committed",
        "reloaded",
        "aborted",
        "invalid",
        "busy",
        "unavailable",
        "unknown",
    )
)
_RECEIPT_STAGES = frozenset(
    ("reserved", "source-applied", "context-committed", "reloaded", "unknown")
)
_MAX_WIRE_BYTES = 64 * 1024


class SourceUpdateError(ValueError):
    """A safe, bounded source-update boundary failure."""


class SourceUpdateBusy(SourceUpdateError):
    """The exact parent is already advancing the same source transaction."""


def _text(value: object, *, field: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 128:
        raise SourceUpdateError(f"invalid {field}")
    if any(ord(character) < 32 for character in value):
        raise SourceUpdateError(f"invalid {field}")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise SourceUpdateError(f"invalid {field}")
    return value


def _revision(value: object, *, field: str, profile: bool = False) -> str:
    return _text(value, field=field, pattern=_PROFILE_REVISION if profile else _SHA)


def _discovery_request(operation: ToolRequest) -> DiscoveryRequest:
    try:
        return DiscoveryRequest.model_validate(
            decode_private_json(
                operation.input_files["MSHIP_TARGET_REQUEST_FILE"].encode()
            )
        )
    except (KeyError, ValueError, TypeError, SessionError) as error:
        raise SourceUpdateError("invalid source-update target request") from error


def _operation(value: object) -> ToolRequest:
    try:
        operation = ToolRequest.from_dict(value)
    except ToolProtocolError as error:
        raise SourceUpdateError("invalid source-update operation") from error
    if (
        operation.preparation != "observe"
        or operation.task_key is None
        or operation.argv
        or operation.env
        or operation.cwd != "."
        or operation.run_ref_repos
        or operation.install_from_result is not None
        or operation.host_tools_action is not None
        or operation.source_revision is None
        or operation.owner_ref is None
        or operation.generation is None
        or operation.task_key.startswith("source-")
        or set(operation.input_files) != {"MSHIP_TARGET_REQUEST_FILE"}
    ):
        raise SourceUpdateError("invalid source-update operation")
    _discovery_request(operation)
    _revision(operation.source_revision, field="expected source revision")
    return operation


@dataclass(frozen=True)
class SourceUpdateReceipt:
    """Safe durable evidence for one non-replayable source transaction."""

    update_id: str
    owner_ref: str
    generation: str
    old_source_revision: str
    new_source_revision: str
    stage: Literal[
        "reserved", "source-applied", "context-committed", "reloaded", "unknown"
    ]

    def __post_init__(self) -> None:
        _text(self.update_id, field="update id", pattern=_ID)
        _text(self.owner_ref, field="owner reference", pattern=_ID)
        _text(self.generation, field="owner generation", pattern=_ID)
        _revision(self.old_source_revision, field="old source revision")
        _revision(self.new_source_revision, field="new source revision")
        if self.stage not in _RECEIPT_STAGES:
            raise SourceUpdateError("invalid source-update receipt stage")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "update_id": self.update_id,
            "owner_ref": self.owner_ref,
            "generation": self.generation,
            "old_source_revision": self.old_source_revision,
            "new_source_revision": self.new_source_revision,
            "stage": self.stage,
        }

    def with_stage(
        self,
        stage: Literal[
            "reserved", "source-applied", "context-committed", "reloaded", "unknown"
        ],
    ) -> "SourceUpdateReceipt":
        return replace(self, stage=stage)

    @classmethod
    def from_dict(cls, value: object) -> "SourceUpdateReceipt":
        if not isinstance(value, Mapping) or set(value) != {
            "update_id",
            "owner_ref",
            "generation",
            "old_source_revision",
            "new_source_revision",
            "stage",
        }:
            raise SourceUpdateError("invalid source-update receipt")
        return cls(
            update_id=_text(value["update_id"], field="update id", pattern=_ID),
            owner_ref=_text(value["owner_ref"], field="owner reference", pattern=_ID),
            generation=_text(
                value["generation"], field="owner generation", pattern=_ID
            ),
            old_source_revision=_revision(
                value["old_source_revision"], field="old source revision"
            ),
            new_source_revision=_revision(
                value["new_source_revision"], field="new source revision"
            ),
            stage=value["stage"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class SourceUpdateRequest:
    operation: ToolRequest
    run_id: str
    update_id: str
    stage: Literal["prepare", "commit", "release", "abort"]
    new_source_revision: str
    new_profile_revision: str

    def __post_init__(self) -> None:
        operation = _operation(self.operation.to_dict())
        object.__setattr__(self, "operation", operation)
        _text(self.run_id, field="run id", pattern=_ID)
        _text(self.update_id, field="update id", pattern=_ID)
        if self.stage not in _REQUEST_STAGES:
            raise SourceUpdateError("invalid source-update stage")
        _revision(self.new_source_revision, field="new source revision")
        _revision(self.new_profile_revision, field="new profile revision", profile=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation.to_dict(),
            "run_id": self.run_id,
            "update_id": self.update_id,
            "stage": self.stage,
            "new_source_revision": self.new_source_revision,
            "new_profile_revision": self.new_profile_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> "SourceUpdateRequest":
        if not isinstance(value, Mapping) or set(value) != {
            "operation",
            "run_id",
            "update_id",
            "stage",
            "new_source_revision",
            "new_profile_revision",
        }:
            raise SourceUpdateError("invalid source-update request")
        try:
            encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        except (TypeError, UnicodeError) as error:
            raise SourceUpdateError("invalid source-update request") from error
        if len(encoded) > _MAX_WIRE_BYTES:
            raise SourceUpdateError("source-update request exceeds size limit")
        return cls(
            operation=_operation(value["operation"]),
            run_id=_text(value["run_id"], field="run id", pattern=_ID),
            update_id=_text(value["update_id"], field="update id", pattern=_ID),
            stage=value["stage"],  # type: ignore[arg-type]
            new_source_revision=_revision(
                value["new_source_revision"], field="new source revision"
            ),
            new_profile_revision=_revision(
                value["new_profile_revision"],
                field="new profile revision",
                profile=True,
            ),
        )


@dataclass(frozen=True)
class SourceUpdateReply:
    update_id: str
    run_id: str
    stage: Literal[
        "prepared",
        "context-committed",
        "reloaded",
        "aborted",
        "invalid",
        "busy",
        "unavailable",
        "unknown",
    ]
    source_revision: str
    profile_revision: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        _text(self.update_id, field="update id", pattern=_ID)
        _text(self.run_id, field="run id", pattern=_ID)
        if self.stage not in _REPLY_STAGES:
            raise SourceUpdateError("invalid source-update reply stage")
        _revision(self.source_revision, field="source revision")
        _revision(self.profile_revision, field="profile revision", profile=True)
        if self.error_code is not None:
            _text(self.error_code, field="error code", pattern=_ID)

    def to_dict(self) -> dict[str, str | None]:
        return {
            "update_id": self.update_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "source_revision": self.source_revision,
            "profile_revision": self.profile_revision,
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, value: object) -> "SourceUpdateReply":
        if not isinstance(value, Mapping) or set(value) != {
            "update_id",
            "run_id",
            "stage",
            "source_revision",
            "profile_revision",
            "error_code",
        }:
            raise SourceUpdateError("invalid source-update reply")
        return cls(
            update_id=_text(value["update_id"], field="update id", pattern=_ID),
            run_id=_text(value["run_id"], field="run id", pattern=_ID),
            stage=value["stage"],  # type: ignore[arg-type]
            source_revision=_revision(
                value["source_revision"], field="source revision"
            ),
            profile_revision=_revision(
                value["profile_revision"], field="profile revision", profile=True
            ),
            error_code=None
            if value["error_code"] is None
            else _text(value["error_code"], field="error code", pattern=_ID),
        )


class SessionSourceUpdateService:
    """Server-side half of the lease/apply/acknowledge/reload protocol."""

    def __init__(self, deps: RemoteExecDeps) -> None:
        self.deps = deps

    def execute(self, request: SourceUpdateRequest) -> SourceUpdateReply:
        try:
            self._validate_configured_reload(request)
            if request.stage == "prepare":
                return self._prepare(request)
            if request.stage == "commit":
                return self._commit(request)
            if request.stage == "release":
                return self._release(request)
            return self._abort(request)
        except SourceUpdateBusy:
            return self._reply(request, "busy", error_code="busy")
        except SourceUpdateError:
            return self._reply(request, "invalid", error_code="invalid")
        except KeyError, LookupError:
            return self._reply(request, "unavailable", error_code="unavailable")
        except SessionError as error:
            stage = "busy" if error.code == "busy" else "unknown"
            return self._reply(request, stage, error_code=stage)
        except RuntimeError:
            return self._reply(request, "unknown", error_code="unknown")

    def _validate_configured_reload(self, request: SourceUpdateRequest) -> None:
        repo = self.deps.config.repos.get(request.operation.repo)
        discovery = _discovery_request(request.operation)
        if (
            repo is None
            or discovery.task != request.operation.task
            or discovery.repo != request.operation.repo
            or discovery.operation != "reload"
            or discovery.target_alias is not None
            or discovery.backend_revision != request.operation.source_revision
        ):
            raise SourceUpdateError("source update does not match its sealed request")
        profile = repo.run_profiles.get(discovery.profile)
        if profile is None or profile.backend != discovery.backend:
            raise SourceUpdateError("source update profile is invalid")
        backend = repo.run_backends.get(discovery.backend)
        if (
            backend is None
            or backend.session_owner != "flutter"
            or backend.operations.get("reload") != request.operation.task_key
            or backend.operations.get("run") in repo.task_outputs
            or discovery.options != profile.options
            or profile_revision(
                profile, backend, prepared_source_revision=discovery.backend_revision
            )
            != discovery.profile_revision
            or profile_revision(
                profile, backend, prepared_source_revision=request.new_source_revision
            )
            != request.new_profile_revision
        ):
            raise SourceUpdateError("source update configuration is invalid")
        owner, inputs = self._parent(request)
        try:
            parent = DiscoveryRequest.model_validate(
                decode_private_json(inputs["MSHIP_TARGET_REQUEST_FILE"].encode())
            )
            sealed = decode_private_json(inputs["MSHIP_TARGET_CONTEXT_FILE"].encode())
        except (KeyError, ValueError, SessionError) as error:
            raise SourceUpdateError("source-update parent is unavailable") from error
        if (
            sealed.get("run_id") != request.run_id
            or sealed.get("session_owner") != "flutter"
            or owner.source_revision
            not in {discovery.backend_revision, request.new_source_revision}
            or parent.backend_revision != owner.source_revision
            or parent.profile_revision
            != profile_revision(
                profile, backend, prepared_source_revision=owner.source_revision
            )
            or parent.model_copy(
                update={
                    "operation": "reload",
                    "target_alias": None,
                    "backend_revision": discovery.backend_revision,
                    "profile_revision": discovery.profile_revision,
                }
            )
            != discovery
        ):
            raise SourceUpdateError("source update belongs to another recorded run")

    def _prepare(self, request: SourceUpdateRequest) -> SourceUpdateReply:
        registry = self._registry()
        owner, _inputs = self._parent(request)
        with self._parent_lock(owner):
            previous = self._read_private_receipt(owner)
            if previous is not None and previous.update_id == request.update_id:
                raise SourceUpdateError("source prepare cannot be replayed")
            self._validate_source_changes(request)
            registry.reserve_source_update(request.operation, request.update_id)
            try:
                receipt = self._receipt(request, owner, "reserved")
                self._write_private_receipt(owner, receipt)
                self._owner_operation(
                    request.operation,
                    owner,
                    "source-reserve",
                    {
                        "update_id": request.update_id,
                        "new_source_revision": request.new_source_revision,
                    },
                )
                self._apply_verified_source(request)
                self._write_private_receipt(owner, receipt.with_stage("source-applied"))
            except Exception:
                self._invalidate(request, owner)
                raise RuntimeError("source apply is uncertain") from None
            return self._reply(request, "prepared")

    def _commit(self, request: SourceUpdateRequest) -> SourceUpdateReply:
        registry = self._registry()
        owner, _inputs = self._parent(request)
        with self._parent_lock(owner):
            previous = self._require_receipt(request, owner, "source-applied")
            if owner.source_revision != previous.old_source_revision:
                raise SourceUpdateError("source commit does not match its parent")
            try:
                self._owner_operation(
                    request.operation,
                    owner,
                    "source-commit",
                    {
                        "update_id": request.update_id,
                        "new_source_revision": request.new_source_revision,
                    },
                )
                owner = registry.commit_source_update(
                    request.operation,
                    request.update_id,
                    source_revision=request.new_source_revision,
                    profile_revision=request.new_profile_revision,
                )
                self._write_private_receipt(
                    owner, previous.with_stage("context-committed")
                )
            except Exception:
                self._invalidate(request, owner)
                raise RuntimeError("source context commit is uncertain") from None
            return self._reply(request, "context-committed")

    def _release(self, request: SourceUpdateRequest) -> SourceUpdateReply:
        registry = self._registry()
        owner, _inputs = self._parent(request)
        with self._parent_lock(owner):
            previous = self._require_receipt(
                request, owner, "context-committed", "reloaded"
            )
            if owner.source_revision != previous.new_source_revision:
                raise SourceUpdateError("source release does not match its parent")
            if previous.stage == "reloaded":
                # A durable successful receipt permits acknowledgement, never
                # another machine reload. Recover only this exact remaining lease.
                try:
                    registry.release_source_update(request.operation, request.update_id)
                except SessionError:
                    status = registry.status(
                        task=request.operation.task,
                        repo=request.operation.repo,
                        owner_ref=owner.owner_ref,
                        generation=owner.generation,
                    )
                    if (
                        status.status != "running"
                        or status.source_revision != request.new_source_revision
                    ):
                        raise SourceUpdateBusy("source owner is not available")
                return self._reply(request, "reloaded")
            try:
                if self._release_intent(owner) == request.update_id:
                    raise RuntimeError("source reload acknowledgement is lost")
                self._write_release_intent(owner, request.update_id)
                self._owner_operation(
                    request.operation,
                    owner,
                    "source-release",
                    {"update_id": request.update_id},
                )
                self._write_private_receipt(owner, previous.with_stage("reloaded"))
                registry.release_source_update(request.operation, request.update_id)
            except Exception:
                self._invalidate(request, owner)
                raise RuntimeError("source reload is uncertain") from None
            return self._reply(request, "reloaded")

    def _abort(self, request: SourceUpdateRequest) -> SourceUpdateReply:
        owner, _inputs = self._parent(request)
        with self._parent_lock(owner):
            previous = self._require_receipt(
                request, owner, "reserved", "source-applied", "context-committed"
            )
            if previous.stage != "reserved":
                self._invalidate(request, owner)
                raise RuntimeError("applied source cannot be silently rolled back")
            try:
                self._owner_operation(
                    request.operation,
                    owner,
                    "source-abort",
                    {"update_id": request.update_id},
                )
                # Retain a non-replayable transaction tombstone even though no
                # source bytes were applied and the old parent remains usable.
                self._write_private_receipt(owner, previous.with_stage("unknown"))
                self._registry().release_source_update(
                    request.operation, request.update_id
                )
            except Exception:
                self._invalidate(request, owner)
                raise RuntimeError("source abort is uncertain") from None
            return self._reply(request, "aborted")

    def _registry(self) -> Any:
        from mship.core.remote_exec import _operations

        return _operations(self.deps)

    def _parent(self, request: SourceUpdateRequest):
        found = self._registry().session_for_owner(
            task=request.operation.task,
            repo=request.operation.repo,
            owner_ref=request.operation.owner_ref,
            generation=request.operation.generation,
        )
        if found is None:
            raise LookupError("source-update owner is unavailable")
        return found

    def _require_receipt(
        self, request: SourceUpdateRequest, owner: object, *stages: str
    ) -> SourceUpdateReceipt:
        receipt = self._read_private_receipt(owner)
        if (
            receipt is None
            or receipt.stage not in stages
            or receipt != self._receipt(request, owner, receipt.stage)
        ):
            raise SourceUpdateError("source-update stage or identity does not match")
        return receipt

    def _invalidate(self, request: SourceUpdateRequest, owner: object) -> None:
        try:
            self._registry().release_source_update(
                request.operation, request.update_id, unknown=True
            )
        except SessionError:
            pass
        try:
            self._write_private_receipt(owner, self._receipt(request, owner, "unknown"))
        except SourceUpdateError:
            pass

    def _owner_operation(
        self, operation: ToolRequest, owner: object, name: str, payload: dict[str, str]
    ) -> None:
        from mship.core.remote_exec import source_owner_operation

        source_owner_operation(operation, owner, name, payload, deps=self.deps)

    def _git_output(
        self,
        request: SourceUpdateRequest,
        argv: tuple[str, ...],
        *,
        source_update_id: str | None,
    ) -> bytes:
        command = ToolRequest(
            task=request.operation.task,
            repo=request.operation.repo,
            argv=("git", *argv),
            preparation="observe",
            source_revision=request.operation.source_revision,
            owner_ref=request.operation.owner_ref,
            generation=request.operation.generation,
        )
        output = bytearray()
        result = None
        for event in self._registry().observe(
            command,
            cancel_event=self.deps.cancel_event,
            spawn=self.deps.shell.spawn_argv,
            source_update_id=source_update_id,
        ):
            if event.kind == "stdout":
                if len(output) + len(event.data) > 64 * 1024:
                    raise SourceUpdateError("source verification output is oversized")
                output.extend(event.data)
            elif event.kind == "result":
                result = event.result
        if result is None or result.status != "completed" or result.exit_code != 0:
            raise SourceUpdateError("source verification failed")
        return bytes(output)

    def _verify_source_base(
        self,
        request: SourceUpdateRequest,
        *,
        source_update_id: str | None,
    ) -> None:
        from mship.core.run_ref import run_ref

        def git(*args: str) -> bytes:
            return self._git_output(request, args, source_update_id=source_update_id)

        repo = self.deps.config.repos[request.operation.repo]
        ref = run_ref(request.operation.task, repo.git_root or request.operation.repo)
        if git("status", "--porcelain", "--untracked-files=normal"):
            raise SourceUpdateError("pinned source worktree is dirty")
        if (
            git("rev-parse", "HEAD").strip()
            != request.operation.source_revision.encode()
        ):
            raise SourceUpdateError("pinned source revision changed")
        if (
            git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()
            != request.new_source_revision.encode()
        ):
            raise SourceUpdateError(
                "pushed source ref does not match committed revision"
            )

    def _validate_source_changes(self, request: SourceUpdateRequest) -> None:
        # Read immutable trees before reserving or interrupting the running app.
        self._verify_source_base(request, source_update_id=None)
        changed = self._git_output(
            request,
            (
                "diff",
                "--name-only",
                "-z",
                "--no-relative",
                "--no-renames",
                request.operation.source_revision,
                request.new_source_revision,
                "--",
            ),
            source_update_id=None,
        )
        if changed and not changed.endswith(b"\0"):
            raise SourceUpdateError("source change identity is unavailable")
        repo = self.deps.config.repos[request.operation.repo]
        declared_names = set()
        if repo.host_tools is not None:
            mise = repo.host_tools.mise
            declared_names.add(Path(mise.manifest).name.casefold())
            if mise.lock is not None:
                declared_names.add(Path(mise.lock).name.casefold())
        for raw in changed.split(b"\0"):
            if not raw:
                continue
            try:
                path = Path(raw.decode("utf-8"))
            except UnicodeDecodeError:
                raise SourceUpdateError(
                    "source change identity is unavailable"
                ) from None
            if path.is_absolute() or ".." in path.parts:
                raise SourceUpdateError("source change identity is unavailable")
            name = path.name.casefold()
            if (
                name in declared_names
                or name.startswith("taskfile")
                or name
                in {
                    "pubspec.yaml",
                    "pubspec.lock",
                    ".packages",
                    "package_config.json",
                    "package.json",
                    "package-lock.json",
                    ".fvmrc",
                    ".tool-versions",
                    "podfile",
                    "gemfile",
                    "makefile",
                    "cmakelists.txt",
                    "dockerfile",
                    ".gitmodules",
                    ".metadata",
                }
                or path.suffix.casefold()
                in {
                    ".yaml",
                    ".yml",
                    ".toml",
                    ".lock",
                    ".gradle",
                    ".kts",
                    ".kt",
                    ".java",
                    ".swift",
                    ".m",
                    ".mm",
                    ".c",
                    ".cc",
                    ".cpp",
                    ".h",
                    ".cmake",
                    ".py",
                    ".sh",
                    ".bash",
                    ".zsh",
                    ".ps1",
                    ".bat",
                    ".cmd",
                }
                or (
                    path.suffix.casefold() != ".dart"
                    and any(
                        part.casefold()
                        in {"android", "ios", "macos", "windows", "linux", "web"}
                        for part in path.parts[:-1]
                    )
                )
                or any(
                    part.casefold() in {".taskfiles", ".mothership", ".mise", ".fvm"}
                    for part in path.parts[:-1]
                )
            ):
                raise SourceUpdateError(
                    "source change requires setup or a fresh launch"
                )

    def _apply_verified_source(self, request: SourceUpdateRequest) -> None:
        self._verify_source_base(request, source_update_id=request.update_id)

        def git(*args: str) -> bytes:
            return self._git_output(request, args, source_update_id=request.update_id)

        git("reset", "--keep", request.new_source_revision)
        if git("rev-parse", "HEAD").strip() != request.new_source_revision.encode():
            raise SourceUpdateError("applied source revision could not be verified")
        if git("status", "--porcelain", "--untracked-files=normal"):
            raise SourceUpdateError("applied source worktree is dirty")

    def _receipt(
        self, request: SourceUpdateRequest, owner: object, stage: Any
    ) -> SourceUpdateReceipt:
        owner_ref = getattr(owner, "owner_ref", request.operation.owner_ref)
        generation = getattr(owner, "generation", request.operation.generation)
        return SourceUpdateReceipt(
            update_id=request.update_id,
            owner_ref=_text(owner_ref, field="owner reference", pattern=_ID),
            generation=_text(generation, field="owner generation", pattern=_ID),
            old_source_revision=request.operation.source_revision or "",
            new_source_revision=request.new_source_revision,
            stage=stage,
        )

    @contextmanager
    def _parent_lock(self, owner: object):
        if fcntl is None:
            raise LookupError("Source updates require a POSIX execution host")
        root = getattr(owner, "private_root", None)
        if not isinstance(root, Path) or not root.is_absolute():
            raise SourceUpdateError("source-update receipt root is unavailable")
        directory = _private_directory(root)
        descriptor = -1
        try:
            descriptor = os.open(
                ".source-update.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise SourceUpdateError("source-update lock is not private")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SourceUpdateBusy("source update is busy") from error
            yield
        finally:
            if descriptor >= 0:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
            os.close(directory)

    def _release_intent(self, owner: object) -> str | None:
        try:
            value = read_private_json(
                self._receipt_path(owner).with_name("source-update-release-intent.json")
            )
        except FileNotFoundError:
            return None
        except (OSError, SessionError) as error:
            raise SourceUpdateError(
                "source-update release intent is unavailable"
            ) from error
        if set(value) != {"update_id"}:
            raise SourceUpdateError("source-update release intent is invalid")
        return _text(value["update_id"], field="update id", pattern=_ID)

    def _write_release_intent(self, owner: object, update_id: str) -> None:
        root = getattr(owner, "private_root", None)
        if not isinstance(root, Path):
            raise SourceUpdateError("source-update receipt root is unavailable")
        try:
            _write_receipt(
                root, "source-update-release-intent.json", {"update_id": update_id}
            )
        except (OSError, SessionError) as error:
            raise SourceUpdateError("could not stage source-update release") from error

    @staticmethod
    def _receipt_path(owner: object) -> Path:
        root = getattr(owner, "private_root", None)
        if not isinstance(root, Path) or not root.is_absolute():
            raise SourceUpdateError("source-update receipt root is unavailable")
        return root / "source-update-receipt.json"

    def _write_private_receipt(
        self, owner: object, receipt: SourceUpdateReceipt
    ) -> None:
        root = getattr(owner, "private_root", None)
        if not isinstance(root, Path):
            raise SourceUpdateError("source-update receipt root is unavailable")
        try:
            _write_receipt(root, "source-update-receipt.json", receipt.to_dict())
        except (OSError, SessionError) as error:
            raise SourceUpdateError("could not stage source-update receipt") from error

    def _read_private_receipt(self, owner: object) -> SourceUpdateReceipt | None:
        path = self._receipt_path(owner)
        try:
            value = read_private_json(path)
        except FileNotFoundError:
            return None
        except (OSError, SessionError) as error:
            raise SourceUpdateError("source-update receipt is unavailable") from error
        try:
            return SourceUpdateReceipt.from_dict(value)
        except (TypeError, ValueError) as error:
            raise SourceUpdateError("source-update receipt is invalid") from error

    @staticmethod
    def _reply(
        request: SourceUpdateRequest,
        stage: Literal[
            "prepared",
            "context-committed",
            "reloaded",
            "aborted",
            "invalid",
            "busy",
            "unavailable",
            "unknown",
        ],
        *,
        error_code: str | None = None,
    ) -> SourceUpdateReply:
        return SourceUpdateReply(
            update_id=request.update_id,
            run_id=request.run_id,
            stage=stage,
            source_revision=request.new_source_revision,
            profile_revision=request.new_profile_revision,
            error_code=error_code,
        )


def update_and_reload(
    run: AppRun,
    operation: ToolRequest,
    *,
    new_source_revision: str,
    new_profile_revision: str,
    store: WorkspaceStore,
    exchange: Callable[[SourceUpdateRequest], SourceUpdateReply],
) -> AppRun:
    """Persist the local half of exactly one source handoff and never replay it."""
    operation = _operation(operation.to_dict())
    discovery = _discovery_request(operation)
    _revision(new_source_revision, field="new source revision")
    _revision(new_profile_revision, field="new profile revision", profile=True)
    if (
        run.status != "active"
        or discovery.backend != run.backend
        or discovery.profile != run.profile
        or discovery.profile_revision != run.profile_revision
        or discovery.operation != "reload"
        or run.owner_ref is None
        or run.owner_generation is None
        or operation.task != run.task_slug
        or operation.repo != run.repo
        or operation.source_revision != run.backend_revision
        or operation.owner_ref != run.owner_ref
        or operation.generation != run.owner_generation
        or new_source_revision == run.backend_revision
    ):
        raise SourceUpdateError("source update does not match the recorded Flutter run")

    update_id = token_urlsafe(24)
    receipt = SourceUpdateReceipt(
        update_id=update_id,
        owner_ref=run.owner_ref,
        generation=run.owner_generation,
        old_source_revision=run.backend_revision,
        new_source_revision=new_source_revision,
        stage="reserved",
    )
    current = _persist_receipt(store, run.id, run.revision, receipt)
    request = SourceUpdateRequest(
        operation=operation,
        run_id=run.id,
        update_id=update_id,
        stage="prepare",
        new_source_revision=new_source_revision,
        new_profile_revision=new_profile_revision,
    )

    def fail(current: AppRun, receipt: SourceUpdateReceipt) -> AppRun:
        # Abort is not a reload retry. If application already began, the host
        # invalidates its exact lease rather than restoring a false old identity.
        try:
            exchange(replace(request, stage="abort"))
        except Exception:
            pass
        return _mark_unknown(store, current, receipt.with_stage("unknown"))

    try:
        prepared = exchange(request)
    except Exception:
        return fail(current, receipt)
    if not _matches(prepared, request, "prepared"):
        return fail(current, receipt)

    applied = receipt.with_stage("source-applied")
    try:
        with store.write(immediate=True) as transaction:
            current = transaction.app_runs.acknowledge_source_update(
                transaction.connection,
                run_id=current.id,
                expected_revision=current.revision,
                receipt=applied.to_dict(),
                new_profile_revision=new_profile_revision,
                now=_now(),
            )
    except Exception:
        return fail(current, applied)

    commit_request = replace(request, stage="commit")
    try:
        committed = exchange(commit_request)
    except Exception:
        return fail(current, applied)
    if not _matches(committed, commit_request, "context-committed"):
        return fail(current, applied)

    committed_receipt = applied.with_stage("context-committed")
    try:
        current = _persist_receipt(
            store, current.id, current.revision, committed_receipt
        )
    except Exception:
        return fail(current, committed_receipt)

    release_request = replace(request, stage="release")
    try:
        reloaded = exchange(release_request)
    except Exception:
        return fail(current, committed_receipt)
    if not _matches(reloaded, release_request, "reloaded"):
        return fail(current, committed_receipt)

    final_receipt = committed_receipt.with_stage("reloaded")
    try:
        with store.write(immediate=True) as transaction:
            return transaction.app_runs.finalize_source_update(
                transaction.connection,
                run_id=current.id,
                expected_revision=current.revision,
                receipt=final_receipt.to_dict(),
                now=_now(),
            )
    except Exception:
        return fail(current, final_receipt)


def _persist_receipt(
    store: WorkspaceStore, run_id: str, revision: int, receipt: SourceUpdateReceipt
) -> AppRun:
    with store.write(immediate=True) as transaction:
        return transaction.app_runs.record_source_update(
            transaction.connection,
            run_id=run_id,
            expected_revision=revision,
            receipt=receipt.to_dict(),
            now=_now(),
        )


def _mark_unknown(
    store: WorkspaceStore, current: AppRun, receipt: SourceUpdateReceipt
) -> AppRun:
    def mark(run: AppRun) -> AppRun:
        with store.write(immediate=True) as transaction:
            return transaction.app_runs.mark_source_update_unknown(
                transaction.connection,
                run_id=run.id,
                expected_revision=run.revision,
                receipt=receipt.to_dict(),
                now=_now(),
            )

    try:
        return mark(current)
    except Exception:
        # Retrying only the local CAS neither invokes the host nor replays a
        # source operation. It may establish unknown only when the exact same
        # receipt remains authoritative after a transient database conflict.
        with store.read() as transaction:
            latest = transaction.app_runs.get(transaction.connection, current.id)
        if latest is None or latest.source_update_receipt is None:
            raise SourceUpdateError("source-update outcome could not be retained")
        if latest.source_update_receipt["update_id"] != receipt.update_id:
            raise SourceUpdateError("source-update identity was replaced")
        if latest.status == "unknown":
            return latest
        if (
            latest.status == "active"
            and latest.source_update_receipt["stage"] == "reloaded"
        ):
            return latest
        try:
            return mark(latest)
        except Exception as error:
            raise SourceUpdateError(
                "source-update outcome could not be retained"
            ) from error


def _matches(
    reply: SourceUpdateReply, request: SourceUpdateRequest, stage: str
) -> bool:
    return (
        isinstance(reply, SourceUpdateReply)
        and reply.update_id == request.update_id
        and reply.run_id == request.run_id
        and reply.stage == stage
        and reply.source_revision == request.new_source_revision
        and reply.profile_revision == request.new_profile_revision
        and reply.error_code is None
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
