"""Durable snapshots for task worktrees retained by close."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mship.core.state import Task


class RetainedWorktreeStoreError(RuntimeError):
    """A retained-worktree record could not be written or decoded safely."""


class RetainedWorktreeStore:
    """Append-only private task snapshots for retained worktrees."""

    _VERSION = 1

    def __init__(self, state_dir: Path) -> None:
        self._directory = Path(state_dir) / "retained-worktrees"

    def retain(self, task: Task) -> Path:
        """Persist an immutable recovery snapshot and return its record path."""
        retained_at = datetime.now(timezone.utc)
        payload = self._payload(task.model_copy(deep=True), retained_at)
        self._prepare_directory()

        while True:
            record_path = self._record_path(retained_at)
            try:
                self._write_new(record_path, payload)
            except FileExistsError:
                # UUID collisions are extraordinarily unlikely, but records are
                # append-only: never replace a pre-existing recovery snapshot.
                continue
            return record_path

    def list_records(self) -> list[tuple[Path, Task]]:
        """Load every retained snapshot, failing closed on malformed records."""
        if not self._directory.exists():
            if not self._directory.is_symlink():
                return []
        if self._directory.is_symlink() or not self._directory.is_dir():
            raise RetainedWorktreeStoreError(
                f"retained worktree path is not a directory: {self._directory}"
            )

        records: list[tuple[Path, Task]] = []
        try:
            paths = sorted(self._directory.glob("*.json"))
        except OSError as exc:
            raise RetainedWorktreeStoreError(
                f"could not list retained worktree records in {self._directory}"
            ) from exc
        for path in paths:
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise RetainedWorktreeStoreError(
                        f"retained worktree record is not a regular file: {path}"
                    )
                raw = json.loads(path.read_text(encoding="utf-8"))
            except RetainedWorktreeStoreError:
                raise
            except (OSError, UnicodeError, ValueError) as exc:
                raise RetainedWorktreeStoreError(
                    f"could not read retained worktree record: {path}"
                ) from exc
            records.append((path, self._decode(path, raw)))
        return records

    def _prepare_directory(self) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._directory.is_symlink() or not self._directory.is_dir():
                raise RetainedWorktreeStoreError(
                    f"retained worktree path is not a directory: {self._directory}"
                )
            self._directory.chmod(0o700)
        except RetainedWorktreeStoreError:
            raise
        except OSError as exc:
            raise RetainedWorktreeStoreError(
                f"could not create retained worktree directory: {self._directory}"
            ) from exc

    @classmethod
    def _payload(cls, task: Task, retained_at: datetime) -> dict[str, Any]:
        task_data = task.model_dump(mode="json")
        task_data["worktrees"] = {
            name: str(path) for name, path in task.worktrees.items()
        }
        task_data["passive_repos"] = sorted(task.passive_repos)
        return {
            "version": cls._VERSION,
            "retained_at": retained_at.isoformat(),
            "task": task_data,
        }

    def _record_path(self, retained_at: datetime) -> Path:
        timestamp = retained_at.strftime("%Y%m%dT%H%M%S%fZ")
        return self._directory / f"retained-{timestamp}-{uuid.uuid4().hex}.json"

    def _write_new(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=".retained-worktree-", suffix=".tmp", dir=self._directory
            )
        except OSError as exc:
            raise RetainedWorktreeStoreError(
                f"could not write retained worktree record: {path}"
            ) from exc
        temporary = Path(temporary_name)
        published = False
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
            published = True
            directory_fd = os.open(self._directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except FileExistsError:
            raise
        except OSError as exc:
            raise RetainedWorktreeStoreError(
                f"could not write retained worktree record: {path}"
            ) from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                if not published:
                    raise

    def _decode(self, path: Path, raw: object) -> Task:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"version", "retained_at", "task"}
        ):
            raise RetainedWorktreeStoreError(
                f"retained worktree record has an invalid schema: {path}"
            )
        if (
            not isinstance(raw["version"], int)
            or isinstance(raw["version"], bool)
            or raw["version"] != self._VERSION
            or not isinstance(raw["retained_at"], str)
        ):
            raise RetainedWorktreeStoreError(
                f"retained worktree record has an unsupported version: {path}"
            )
        try:
            retained_at = datetime.fromisoformat(raw["retained_at"])
        except ValueError as exc:
            raise RetainedWorktreeStoreError(
                f"retained worktree record has an invalid retention time: {path}"
            ) from exc
        if retained_at.tzinfo is None:
            raise RetainedWorktreeStoreError(
                "retained worktree record has a timezone-less retention time: "
                f"{path}"
            )
        task_data = raw["task"]
        if (
            not isinstance(task_data, dict)
            or set(task_data) != set(Task.model_fields)
        ):
            raise RetainedWorktreeStoreError(
                f"retained worktree record has an incomplete task snapshot: {path}"
            )
        try:
            return Task.model_validate(task_data)
        except (TypeError, ValueError) as exc:
            raise RetainedWorktreeStoreError(
                f"retained worktree record has an invalid task snapshot: {path}"
            ) from exc
