"""Versioned, private layered run-host registration and resolution."""
from __future__ import annotations

import fcntl
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Literal, Mapping, Sequence

import yaml

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.run_host.config import HostRegistration, MigrationReport, RunHostConnection
from mship.core.run_host.paths import run_host_config_dir

Scope = Literal["user", "project"]
_VERSION = 1


class RunHostError(Exception):
    """Safe, actionable run-host registration or resolution failure."""


class _MigrationRequired(RunHostError):
    pass


def _env_key(role: str, field: str) -> str:
    normalized = role.upper().replace("-", "_")
    return f"MSHIP_RUN_HOST_{normalized}_{field}"


def _connection(raw: object, *, scope: Scope, name: str) -> RunHostConnection:
    if not isinstance(raw, Mapping):
        raise RunHostError(f"invalid {scope} run-host entry {name!r}: missing connection")
    url, token = raw.get("url"), raw.get("token")
    if not isinstance(url, str) or not url or not isinstance(token, str) or not token:
        raise RunHostError(f"invalid {scope} run-host entry {name!r}: connection requires url and token")
    return RunHostConnection(url, token)


def _as_strings(raw: object, *, field: str, scope: Scope, name: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(value, str) and value for value in raw):
        raise RunHostError(f"invalid {scope} run-host entry {name!r}: {field} must be a list of names")
    return tuple(raw)


def _parse_v1(raw: object, *, scope: Scope) -> tuple[dict[str, HostRegistration], dict[str, tuple[str, ...]]]:
    if not isinstance(raw, Mapping):
        raise RunHostError(f"invalid {scope} run-host registry: expected a mapping")
    if raw.get("version") != _VERSION:
        if "version" not in raw:
            raise _MigrationRequired(
                f"{scope} run-host registry uses the legacy format; run "
                f"`mship run-host migrate --scope {scope} --apply`"
            )
        raise RunHostError(f"unsupported {scope} run-host registry version")
    hosts = raw.get("hosts")
    if not isinstance(hosts, Mapping):
        raise RunHostError(f"invalid {scope} run-host registry: hosts must be a mapping")
    unknown = set(raw) - {"version", "hosts", "role_hosts"}
    if unknown:
        raise RunHostError(f"invalid {scope} run-host registry: unknown keys {sorted(unknown)}")
    parsed: dict[str, HostRegistration] = {}
    for name, entry in hosts.items():
        if not isinstance(name, str) or not name or not isinstance(entry, Mapping):
            raise RunHostError(f"invalid {scope} run-host registry host entry")
        unknown_entry = set(entry) - {"roles", "tags", "preference", "connection"}
        if unknown_entry:
            raise RunHostError(f"invalid {scope} run-host entry {name!r}: unknown keys {sorted(unknown_entry)}")
        roles = _as_strings(entry.get("roles"), field="roles", scope=scope, name=name)
        tags = _as_strings(entry.get("tags", []), field="tags", scope=scope, name=name)
        preference = entry.get("preference", 0)
        if not isinstance(preference, int) or isinstance(preference, bool):
            raise RunHostError(f"invalid {scope} run-host entry {name!r}: preference must be an integer")
        parsed[name] = HostRegistration(name, roles, tags, preference, _connection(entry.get("connection"), scope=scope, name=name), scope)
    policy_raw = raw.get("role_hosts", {})
    if scope == "user" and "role_hosts" in raw:
        raise RunHostError("invalid user run-host registry: role_hosts is project-only")
    if not isinstance(policy_raw, Mapping):
        raise RunHostError(f"invalid {scope} run-host registry: role_hosts must be a mapping")
    policy: dict[str, tuple[str, ...]] = {}
    for role, names in policy_raw.items():
        if not isinstance(role, str) or not role or not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
            raise RunHostError(f"invalid {scope} run-host role_hosts entry")
        policy[role] = tuple(names)
    return parsed, policy


def _legacy(raw: object, *, scope: Scope) -> dict[str, RunHostConnection | None]:
    if not isinstance(raw, Mapping):
        raise RunHostError(f"invalid {scope} legacy run-host registry")
    result: dict[str, RunHostConnection | None] = {}
    for role, entry in raw.items():
        if not isinstance(role, str) or not role:
            raise RunHostError(f"invalid {scope} legacy run-host role")
        if not isinstance(entry, Mapping):
            raise RunHostError(f"invalid {scope} legacy run-host entry {role!r}")
        url, token = entry.get("url"), entry.get("token")
        if url is None and token is None:
            result[role] = None
        elif isinstance(url, str) and url and isinstance(token, str) and token:
            result[role] = RunHostConnection(url, token)
        else:
            raise RunHostError(f"invalid {scope} legacy run-host entry {role!r}")
    return result


class RunHostStore:
    """Layer user XDG hosts with complete, shadowing project host entries."""

    def __init__(self, state_dir: Path, *, user_config_dir: Path | None = None) -> None:
        self._project_path = Path(state_dir) / "run-hosts.yaml"
        config_dir = user_config_dir or run_host_config_dir(Path.home(), os.environ)
        self._user_path = Path(config_dir) / "run-hosts.yaml"
        self._path = self._project_path  # compatibility for callers that surface this path

    def _path_for(self, scope: Scope) -> Path:
        return self._user_path if scope == "user" else self._project_path

    def _lock(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = path.with_name(path.name + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock, 0o600)
        return fd

    def _read_scope(self, scope: Scope, *, migration: bool = False) -> tuple[dict[str, HostRegistration], dict[str, tuple[str, ...]]]:
        path = self._path_for(scope)
        if not path.exists():
            return {}, {}
        try:
            raw = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError, UnicodeError) as exc:
            raise RunHostError(f"could not read {scope} run-host registry") from exc
        raw = raw or {}
        if migration and isinstance(raw, Mapping) and "version" not in raw:
            return {}, {}
        return _parse_v1(raw, scope=scope)

    def _legacy_scope(self, scope: Scope) -> dict[str, RunHostConnection | None] | None:
        path = self._path_for(scope)
        if not path.exists():
            return None
        raw = yaml.safe_load(path.read_text()) or {}
        if isinstance(raw, Mapping) and "version" in raw:
            return None
        return _legacy(raw, scope=scope)

    @staticmethod
    def _document(hosts: Mapping[str, HostRegistration], role_hosts: Mapping[str, Sequence[str]] | None = None) -> dict:
        document = {"version": _VERSION, "hosts": {}}
        for name, host in hosts.items():
            document["hosts"][name] = {
                "roles": list(host.roles), "tags": list(host.tags), "preference": host.preference,
                "connection": {"url": host.connection.url, "token": host.connection.token},
            }
        if role_hosts is not None:
            document["role_hosts"] = {role: list(names) for role, names in role_hosts.items()}
        return document

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(path)
            os.chmod(path, 0o600)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    @classmethod
    def _atomic_write(cls, path: Path, document: dict) -> None:
        cls._atomic_write_bytes(path, yaml.safe_dump(document, sort_keys=True).encode())

    def _mutate(self, scope: Scope, mutation) -> None:
        path = self._path_for(scope)
        lock_fd = self._lock(path)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            hosts, policy = self._read_scope(scope)
            mutation(hosts, policy)
            self._atomic_write(path, self._document(hosts, policy if scope == "project" else None))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def effective_hosts(self) -> dict[str, HostRegistration]:
        user_hosts, _ = self._read_scope("user")
        project_hosts, _ = self._read_scope("project")
        return {**user_hosts, **project_hosts}

    def role_hosts(self) -> dict[str, tuple[str, ...]]:
        _, policy = self._read_scope("project")
        return policy

    def set_host(self, host: HostRegistration, *, scope: Scope) -> None:
        if not host.name or not host.roles:
            raise RunHostError("run-host name and at least one role are required")
        self._mutate(scope, lambda hosts, _policy: hosts.__setitem__(host.name, replace(host, scope=scope)))

    def remove_host(self, name: str, *, scope: Scope) -> None:
        self._mutate(scope, lambda hosts, _policy: hosts.pop(name, None))

    def set_role_hosts(self, role: str, names: Sequence[str] | None) -> None:
        if not role:
            raise RunHostError("role is required")
        def mutate(_hosts, policy):
            if names is None:
                policy.pop(role, None)
            else:
                policy[role] = tuple(names)
        self._mutate("project", mutate)

    def migrate(self, *, scope: Scope, allowed_roles: Sequence[str], apply: bool) -> MigrationReport:
        path = self._path_for(scope)
        lock_fd = self._lock(path)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            legacy = self._legacy_scope(scope)
            if legacy is None:
                return MigrationReport(scope, False, ())
            hosts: dict[str, HostRegistration] = {}
            policy: dict[str, tuple[str, ...]] = {}
            for role, conn in legacy.items():
                if conn is not None:
                    hosts[role] = HostRegistration(role, (role,), (), 0, conn, scope)
                    if scope == "project":
                        policy[role] = (role,)
                elif scope == "project":
                    policy[role] = ()
            if scope == "project":
                for role in allowed_roles:
                    policy.setdefault(role, ())
            backup = path.with_name(path.name + ".legacy.bak")
            if apply:
                # The backup is byte-for-byte exact and installed before the
                # atomically replaced active file.
                self._atomic_write_bytes(backup, path.read_bytes())
                self._atomic_write(path, self._document(hosts, policy if scope == "project" else None))
            return MigrationReport(scope, True, tuple(sorted(hosts)), backup if apply else None)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    # Legacy method names deliberately create versioned entries; they do not read legacy files.
    def get(self, role: str) -> RunHostConnection | None:
        try:
            return _connection_for_role(self, role, environ=os.environ)
        except _MigrationRequired:
            return None

    def set(self, role: str, conn: RunHostConnection) -> None:
        self.set_host(HostRegistration(role, (role,), (), 0, conn, "project"), scope="project")

    def remove(self, role: str) -> None:
        self.remove_host(role, scope="project")

    def redacted_list(self) -> list[tuple[str, str]]:
        return sorted((name, host.connection.url) for name, host in self.effective_hosts().items())

    def safe_hosts(self) -> dict[str, dict[str, object]]:
        return {name: {"name": host.name, "roles": host.roles, "tags": host.tags,
                       "preference": host.preference, "scope": host.scope,
                       "url": host.connection.url}
                for name, host in self.effective_hosts().items()}


    def connection_for_role(self, role: str, *, environ: Mapping[str, str]) -> RunHostConnection | None:
        """Resolve a role's private connection for a trusted in-process caller."""
        return _connection_for_role(self, role, environ=environ)

def _connection_for_role(store: RunHostStore, role: str, *, environ: Mapping[str, str]) -> RunHostConnection | None:
    hosts = store.effective_hosts()
    policy = store.role_hosts()
    candidates = [host for host in hosts.values() if role in host.roles]
    if role in policy:
        candidates = [host for host in candidates if host.name in policy[role]]
    url, token = environ.get(_env_key(role, "URL")), environ.get(_env_key(role, "TOKEN"))
    if len(candidates) > 1:
        raise RunHostError(f"ambiguous run-host role {role!r}; eligible hosts: {sorted(host.name for host in candidates)}")
    if len(candidates) == 1:
        base = candidates[0].connection
        return RunHostConnection(url or base.url, token or base.token)
    # Legacy environment-only registration remains supported for a declared role,
    # but a project role policy that explicitly permits none must not be bypassed.
    if role not in policy and url and token:
        return RunHostConnection(url, token)
    return None


def resolve_run_host(role: str | None, *, repo: RepoConfig | None, config: WorkspaceConfig, store: RunHostStore) -> RunHostConnection:
    known = tuple(config.run_hosts)
    if role is not None:
        chosen = role
    elif repo is not None and repo.run_host:
        chosen = repo.run_host
    elif len(known) == 1:
        chosen = known[0]
    elif not known:
        raise RunHostError("no run_hosts declared in mothership.yaml; add a `run_hosts:` list before using --remote")
    else:
        raise RunHostError(f"ambiguous run-host: multiple roles are configured ({', '.join(known)}) and none was specified; pass --remote=<role> to pick one")
    if chosen not in known:
        raise RunHostError(f"unknown run-host role {chosen!r}; not declared in this workspace's `run_hosts:` list. Declared roles: {sorted(known)}")
    normalized = _env_key(chosen, "URL")
    collisions = [candidate for candidate in known if _env_key(candidate, "URL") == normalized]
    if len(collisions) > 1:
        raise RunHostError(f"ambiguous environment normalization for roles {sorted(collisions)}")
    try:
        conn = _connection_for_role(store, chosen, environ=os.environ)
    except _MigrationRequired as exc:
        raise RunHostError(str(exc)) from exc
    if conn is None:
        raise RunHostError(f"run-host role {chosen!r} is declared but has no eligible connection mapped on this machine; run `mship run-host add {chosen}` or migrate the legacy registry")
    return conn
