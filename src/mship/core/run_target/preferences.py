"""Owner-private, project-local remembered target preferences."""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
import tempfile
from typing import Mapping

import yaml

from mship.core.run_target.models import TargetSelectionError, safe_identifier


_VERSION = 1
_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class TargetPreference:
    """A late tie preference expressed only in safe friendly names."""

    host_name: str | None
    target_alias: str | None

    def __post_init__(self) -> None:
        if self.host_name is None and self.target_alias is None:
            raise ValueError("a target preference needs a host name or target alias")
        if self.host_name is not None:
            safe_identifier(self.host_name, field="preference host name")
        if self.target_alias is not None:
            safe_identifier(self.target_alias, field="preference target alias")


class TargetPreferenceStore:
    """Flock-protected preferences at ``.mothership/run-target-preferences.yaml``.

    This store never persists backend keys or bindings.  The orchestrator passes
    remembered preferences only when no explicit host or target constraint exists.
    """

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "run-target-preferences.yaml"
        self._lock_path = self._path.with_name(self._path.name + ".lock")

    @staticmethod
    def _key(value: str, *, field: str) -> str:
        return safe_identifier(value, field=field)

    def _lock(self, mode: int):
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(self._lock_path, 0o600)
        fcntl.flock(descriptor, mode)
        return descriptor

    @staticmethod
    def _invalid() -> TargetSelectionError:
        return TargetSelectionError("preferences_invalid", "could not read private target preferences")

    def _read_nolock(self) -> dict[str, dict[str, TargetPreference]]:
        try:
            descriptor = os.open(self._path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise self._invalid() from exc
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_size > _MAX_BYTES
            ):
                raise self._invalid()
            chunks: list[bytes] = []
            remaining = _MAX_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > _MAX_BYTES:
                raise self._invalid()
            document = yaml.safe_load(raw.decode("utf-8"))
        except TargetSelectionError:
            raise
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise self._invalid() from exc
        finally:
            os.close(descriptor)
        if not isinstance(document, Mapping) or set(document) != {"version", "preferences"}:
            raise self._invalid()
        if document["version"] != _VERSION or isinstance(document["version"], bool):
            raise self._invalid()
        preferences = document["preferences"]
        if not isinstance(preferences, Mapping):
            raise self._invalid()
        parsed: dict[str, dict[str, TargetPreference]] = {}
        try:
            for repo, profiles in preferences.items():
                repo_name = self._key(repo, field="preference repo") if isinstance(repo, str) else None
                if repo_name is None or not isinstance(profiles, Mapping):
                    raise ValueError("invalid preference repository")
                parsed[repo_name] = {}
                for profile, raw_preference in profiles.items():
                    profile_name = self._key(profile, field="preference profile") if isinstance(profile, str) else None
                    if profile_name is None or not isinstance(raw_preference, Mapping):
                        raise ValueError("invalid preference profile")
                    if set(raw_preference) != {"host_name", "target_alias"}:
                        raise ValueError("invalid preference fields")
                    host_name = raw_preference["host_name"]
                    target_alias = raw_preference["target_alias"]
                    if not isinstance(host_name, str | type(None)) or not isinstance(target_alias, str | type(None)):
                        raise ValueError("invalid preference values")
                    parsed[repo_name][profile_name] = TargetPreference(host_name, target_alias)
        except (TypeError, ValueError) as exc:
            raise self._invalid() from exc
        return parsed

    def _write_nolock(self, preferences: Mapping[str, Mapping[str, TargetPreference]]) -> None:
        document = {
            "version": _VERSION,
            "preferences": {
                repo: {
                    profile: {"host_name": preference.host_name, "target_alias": preference.target_alias}
                    for profile, preference in profiles.items()
                }
                for repo, profiles in preferences.items()
            },
        }
        payload = yaml.safe_dump(document, sort_keys=True).encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent)
        temp = Path(temp_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self._path)
            os.chmod(self._path, 0o600)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def get(self, repo: str, profile: str) -> TargetPreference | None:
        repo_name = self._key(repo, field="preference repo")
        profile_name = self._key(profile, field="preference profile")
        descriptor = self._lock(fcntl.LOCK_SH)
        try:
            return self._read_nolock().get(repo_name, {}).get(profile_name)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def put(self, repo: str, profile: str, preference: TargetPreference) -> None:
        if not isinstance(preference, TargetPreference):
            raise TypeError("preference must be a TargetPreference")
        repo_name = self._key(repo, field="preference repo")
        profile_name = self._key(profile, field="preference profile")
        descriptor = self._lock(fcntl.LOCK_EX)
        try:
            preferences = self._read_nolock()
            preferences.setdefault(repo_name, {})[profile_name] = preference
            self._write_nolock(preferences)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def remove(self, repo: str, profile: str) -> None:
        repo_name = self._key(repo, field="preference repo")
        profile_name = self._key(profile, field="preference profile")
        descriptor = self._lock(fcntl.LOCK_EX)
        try:
            preferences = self._read_nolock()
            profiles = preferences.get(repo_name)
            if profiles is None or profile_name not in profiles:
                return
            del profiles[profile_name]
            if not profiles:
                del preferences[repo_name]
            self._write_nolock(preferences)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
