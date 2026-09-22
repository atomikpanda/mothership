"""Server-only preparation of private capabilities for supervised app tasks."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from mship.core.session_inputs import InstallFromResult, InstallGrant, SessionError

if TYPE_CHECKING:
    from mship.core.task_results import TaskResultStore

SESSION_RESERVED_INPUTS = frozenset(
    {
        "MSHIP_OWNER_CONTEXT_FILE",
        "MSHIP_INSTALL_GRANT_FILE",
        "MSHIP_CAPTURE_GRANT_FILE",
    }
)
SESSION_CAPTURE_ENV = frozenset(
    {"MSHIP_CAPTURE_DIR", "MSHIP_CAPTURE_KINDS", "MSHIP_CAPTURE_PLATFORM"}
)


@dataclass(frozen=True)
class SessionPreparation:
    """An internal server capability, never decoded from the public request."""

    operation: str
    owner_kind: str | None
    sealed_context: str | None = field(default=None, repr=False)
    install: InstallFromResult | None = None
    result_store: TaskResultStore | None = field(default=None, repr=False)
    capture_kinds: tuple[str, ...] | None = None
    capture_platform: str | None = None

    def __post_init__(self) -> None:
        if self.owner_kind not in {None, "android", "flutter"}:
            raise SessionError("invalid", "Invalid configured session owner")
        if self.owner_kind is None:
            if (
                self.operation != "run"
                or self.sealed_context is None
                or self.install is not None
                or self.capture_kinds is not None
                or self.capture_platform is not None
            ):
                raise SessionError("invalid", "Invalid generic session")
            return
        if self.install is not None and self.owner_kind != "android":
            raise SessionError(
                "invalid", "Flutter cannot consume a native installation grant"
            )
        if (self.capture_kinds is None) != (self.capture_platform is None):
            raise SessionError("invalid", "Incomplete capture request")
        if self.capture_kinds is not None and self.operation != "capture":
            raise SessionError("invalid", "Capture grant requires capture operation")


def prepare_session_install(
    store: TaskResultStore | None, requested: InstallFromResult, private_root: Path
) -> InstallGrant:
    """Copy only the canonical reader's verified bytes into an owned grant."""
    from mship.core.task_results import TaskResultError

    if store is None:
        raise SessionError(
            "unavailable", "Immutable installation results are unavailable"
        )
    destination: Path | None = None
    directory: int | None = None
    try:
        result = store.get(requested.result_id)
        if result.outcome.status != "completed" or result.outcome.exit_code != 0:
            raise SessionError(
                "invalid", "Installation requires a successful immutable result"
            )
        with store.open_verified(
            requested.result_id, requested.artifact_id
        ) as verified:
            artifact = verified.artifact
            if (
                artifact.availability != "published"
                or artifact.sha256 != requested.sha256
                or artifact.byte_size is None
                or artifact.byte_size <= 0
            ):
                raise SessionError(
                    "invalid", "Installation artifact identity does not match"
                )
            directory = os.open(
                private_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SessionError("invalid", "Installation staging is not private")
            filename = f"install-{secrets.token_hex(16)}.apk"
            fd = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o400,
                dir_fd=directory,
            )
            destination = private_root / filename
            digest = hashlib.sha256()
            count = 0
            try:
                while chunk := os.read(verified.fd, 1024 * 1024):
                    count += len(chunk)
                    if count > artifact.byte_size:
                        raise SessionError(
                            "invalid", "Installation artifact changed during staging"
                        )
                    digest.update(chunk)
                    pending = memoryview(chunk)
                    while pending:
                        written = os.write(fd, pending)
                        if written <= 0:
                            raise OSError("short installation write")
                        pending = pending[written:]
                os.fsync(fd)
                copied = os.fstat(fd)
                if (
                    not stat.S_ISREG(copied.st_mode)
                    or copied.st_nlink != 1
                    or count != artifact.byte_size
                    or digest.hexdigest() != requested.sha256
                ):
                    raise SessionError(
                        "invalid", "Installation artifact failed staging verification"
                    )
            finally:
                os.close(fd)
            os.fsync(directory)
            return InstallGrant(requested, destination, count)
    except (OSError, TaskResultError) as error:
        if destination is not None and directory is not None:
            try:
                os.unlink(destination.name, dir_fd=directory)
            except FileNotFoundError:
                pass
        raise SessionError(
            "unavailable", "Verified installation artifact is unavailable"
        ) from error
    except BaseException:
        if destination is not None and directory is not None:
            try:
                os.unlink(destination.name, dir_fd=directory)
            except FileNotFoundError:
                pass
        raise
    finally:
        if directory is not None:
            os.close(directory)
