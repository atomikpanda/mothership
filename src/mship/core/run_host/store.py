"""Versioned, private layered run-host registration and selection."""
from __future__ import annotations

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping, Sequence

import yaml

from mship.core.run_host.config import (
    RunHostConnection,
    HostRegistration,
    MigrationReport,
    RelayRunHostIdentity,
    RunHostConnection,
)
from mship.core.run_host.paths import run_host_config_dir

if TYPE_CHECKING:
    from mship.core.config import RepoConfig, WorkspaceConfig

Scope = Literal["user", "project"]
_VERSION = 2


class RunHostError(Exception):
    """Safe, actionable run-host registration or resolution failure."""


class _MigrationRequired(RunHostError):
    pass


def _env_key(role: str, field: str) -> str:
    return f"MSHIP_RUN_HOST_{role.upper().replace('-', '_')}_{field}"


def _as_strings(raw: object, *, field: str, scope: Scope, name: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(value, str) and value for value in raw):
        raise RunHostError(f"invalid {scope} run-host entry {name!r}: {field} must be a list of names")
    return tuple(raw)


def _connection(raw: object, *, scope: Scope, name: str) -> RunHostConnection | RelayRunHostIdentity:
    if not isinstance(raw, Mapping):
        raise RunHostError(f"invalid {scope} run-host entry {name!r}: missing connection")
    mode = raw.get("mode")
    if mode == "direct" and set(raw) == {"mode", "url", "token"}:
        try:
            return RunHostConnection(raw["url"], raw["token"])
        except (KeyError, ValueError, TypeError) as exc:
            raise RunHostError(f"invalid {scope} run-host entry {name!r}: direct connection requires url and token") from exc
    if mode == "relay" and set(raw) == {"mode", "relay", "host_id", "workspace_id", "instance_id"}:
        try:
            return RelayRunHostIdentity(raw["relay"], raw["host_id"], raw["workspace_id"], raw["instance_id"])
        except (KeyError, ValueError, TypeError) as exc:
            raise RunHostError(f"invalid {scope} run-host entry {name!r}: relay identity is incomplete") from exc
    raise RunHostError(f"invalid {scope} run-host entry {name!r}: connection must be exactly direct or relay")


def _parse_document(raw: object, *, scope: Scope) -> tuple[dict[str, HostRegistration], dict[str, tuple[str, ...]]]:
    if not isinstance(raw, Mapping):
        raise RunHostError(f"invalid {scope} run-host registry: expected a mapping")
    version = raw.get("version")
    if version != _VERSION:
        if version == 1 or "version" not in raw:
            raise _MigrationRequired(
                f"{scope} run-host registry uses a legacy direct format; run `mship run-host migrate --scope {scope} --apply`"
            )
        raise RunHostError(f"unsupported {scope} run-host registry version")
    if set(raw) - {"version", "hosts", "role_hosts"}:
        raise RunHostError(f"invalid {scope} run-host registry: unknown keys")
    hosts_raw = raw.get("hosts")
    if not isinstance(hosts_raw, Mapping):
        raise RunHostError(f"invalid {scope} run-host registry: hosts must be a mapping")
    parsed: dict[str, HostRegistration] = {}
    for name, entry in hosts_raw.items():
        if not isinstance(name, str) or not name or not isinstance(entry, Mapping):
            raise RunHostError(f"invalid {scope} run-host registry host entry")
        if set(entry) - {"roles", "tags", "preference", "connection"}:
            raise RunHostError(f"invalid {scope} run-host entry {name!r}: unknown keys")
        preference = entry.get("preference", 0)
        if not isinstance(preference, int) or isinstance(preference, bool):
            raise RunHostError(f"invalid {scope} run-host entry {name!r}: preference must be an integer")
        parsed[name] = HostRegistration(
            name,
            _as_strings(entry.get("roles"), field="roles", scope=scope, name=name),
            _as_strings(entry.get("tags", []), field="tags", scope=scope, name=name),
            preference,
            _connection(entry.get("connection"), scope=scope, name=name),
            scope,
        )
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
    if raw.get("version") == 1:
        hosts = raw.get("hosts")
        if not isinstance(hosts, Mapping):
            raise RunHostError(f"invalid {scope} legacy run-host registry")
        for name, entry in hosts.items():
            if not isinstance(name, str) or not name or not isinstance(entry, Mapping):
                raise RunHostError(f"invalid {scope} legacy run-host registry")
            connection = entry.get("connection")
            if not isinstance(connection, Mapping) or set(connection) != {"url", "token"}:
                raise RunHostError(f"invalid {scope} legacy run-host entry {name!r}")
            try:
                result[name] = RunHostConnection(connection["url"], connection["token"])
            except (KeyError, ValueError, TypeError) as exc:
                raise RunHostError(f"invalid {scope} legacy run-host entry {name!r}") from exc
        return result
    for role, entry in raw.items():
        if not isinstance(role, str) or not role or not isinstance(entry, Mapping):
            raise RunHostError(f"invalid {scope} legacy run-host registry")
        url, token = entry.get("url"), entry.get("token")
        if url is None and token is None:
            result[role] = None
        else:
            try:
                result[role] = RunHostConnection(url, token)
            except (ValueError, TypeError) as exc:
                raise RunHostError(f"invalid {scope} legacy run-host entry {role!r}") from exc
    return result


def _version_one_hosts(
    raw: object, *, scope: Scope
) -> tuple[dict[str, HostRegistration], dict[str, tuple[str, ...]]] | None:
    """Read the immediately previous, direct-only schema without reinterpretation."""
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        return None
    hosts_raw = raw.get("hosts")
    if not isinstance(hosts_raw, Mapping):
        raise RunHostError(f"invalid {scope} legacy run-host registry")
    hosts: dict[str, HostRegistration] = {}
    for name, entry in hosts_raw.items():
        if not isinstance(name, str) or not name or not isinstance(entry, Mapping):
            raise RunHostError(f"invalid {scope} legacy run-host registry")
        connection = entry.get("connection")
        try:
            direct = RunHostConnection(connection["url"], connection["token"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RunHostError(f"invalid {scope} legacy run-host entry {name!r}") from exc
        roles = _as_strings(entry.get("roles"), field="roles", scope=scope, name=name)
        tags = _as_strings(entry.get("tags", []), field="tags", scope=scope, name=name)
        preference = entry.get("preference", 0)
        if not isinstance(preference, int) or isinstance(preference, bool):
            raise RunHostError(f"invalid {scope} legacy run-host entry {name!r}")
        hosts[name] = HostRegistration(name, roles, tags, preference, direct, scope)
    policy_raw = raw.get("role_hosts", {})
    if not isinstance(policy_raw, Mapping):
        raise RunHostError(f"invalid {scope} legacy run-host registry")
    policy = {role: tuple(names) for role, names in policy_raw.items()
              if isinstance(role, str) and role and isinstance(names, list)
              and all(isinstance(name, str) and name for name in names)}
    if len(policy) != len(policy_raw):
        raise RunHostError(f"invalid {scope} legacy run-host registry")
    return hosts, policy


class RunHostStore:
    """Layer user XDG hosts with complete, shadowing project host entries."""
    def __init__(self, state_dir: Path, *, user_config_dir: Path | None = None) -> None:
        self._project_path = Path(state_dir) / "run-hosts.yaml"
        config_dir = user_config_dir or run_host_config_dir(Path.home(), os.environ)
        self._user_path = Path(config_dir) / "run-hosts.yaml"
        self._path = self._project_path

    def _path_for(self, scope: Scope) -> Path:
        return self._user_path if scope == "user" else self._project_path

    @staticmethod
    def _require_posix_locking() -> None:
        if fcntl is None:
            raise RunHostError("run-host registry mutation requires POSIX file locking; this platform cannot safely update private host registrations")

    def _lock(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        lock = path.with_name(path.name + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock, 0o600)
        return fd

    def _read_scope(self, scope: Scope) -> tuple[dict[str, HostRegistration], dict[str, tuple[str, ...]]]:
        path = self._path_for(scope)
        if not path.exists():
            return {}, {}
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError, UnicodeError) as exc:
            raise RunHostError(f"could not read {scope} run-host registry") from exc
        return _parse_document(raw, scope=scope)

    def _legacy_scope(self, scope: Scope) -> dict[str, RunHostConnection | None] | None:
        path = self._path_for(scope)
        if not path.exists():
            return None
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError, UnicodeError) as exc:
            raise RunHostError(f"could not read {scope} run-host registry") from exc
        if isinstance(raw, Mapping) and raw.get("version") == _VERSION:
            return None
        return _legacy(raw, scope=scope)

    @staticmethod
    def _document(hosts: Mapping[str, HostRegistration], role_hosts: Mapping[str, Sequence[str]] | None = None) -> dict:
        document: dict[str, object] = {"version": _VERSION, "hosts": {}}
        entries: dict[str, object] = document["hosts"]  # type: ignore[assignment]
        for name, host in hosts.items():
            if isinstance(host.connection, RunHostConnection):
                connection = {"mode": "direct", "url": host.connection.url, "token": host.connection.token}
            else:
                connection = {"mode": "relay", "relay": host.connection.relay, "host_id": host.connection.host_id, "workspace_id": host.connection.workspace_id, "instance_id": host.connection.instance_id}
            entries[name] = {"roles": list(host.roles), "tags": list(host.tags), "preference": host.preference, "connection": connection}
        if role_hosts is not None:
            document["role_hosts"] = {role: list(names) for role, names in role_hosts.items()}
        return document

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
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
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    @classmethod
    def _atomic_write(cls, path: Path, document: dict) -> None:
        cls._atomic_write_bytes(path, yaml.safe_dump(document, sort_keys=True).encode())

    def _mutate(self, scope: Scope, mutation) -> None:
        self._require_posix_locking()
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
        self._mutate("project", lambda _hosts, policy: policy.pop(role, None) if names is None else policy.__setitem__(role, tuple(names)))

    def migrate(self, *, scope: Scope, allowed_roles: Sequence[str], apply: bool) -> MigrationReport:
        self._require_posix_locking()
        path = self._path_for(scope)
        lock_fd = self._lock(path)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                raw = yaml.safe_load(path.read_text()) if path.exists() else None
            except (OSError, yaml.YAMLError, UnicodeError) as exc:
                raise RunHostError(f"could not read {scope} run-host registry") from exc
            previous = _version_one_hosts(raw, scope=scope)
            if previous is not None:
                hosts, policy = previous
            else:
                legacy = self._legacy_scope(scope)
                if legacy is None:
                    return MigrationReport(scope, False, ())
                hosts = {}
                policy = {}
                for role, connection in legacy.items():
                    if connection is not None:
                        hosts[role] = HostRegistration(role, (role,), (), 0, connection, scope)
                        if scope == "project":
                            policy[role] = (role,)
                    elif scope == "project":
                        policy[role] = ()
                if scope == "project":
                    for role in allowed_roles:
                        policy.setdefault(role, ())
            backup = path.with_name(path.name + ".legacy.bak")
            if apply:
                self._atomic_write_bytes(backup, path.read_bytes())
                self._atomic_write(path, self._document(hosts, policy if scope == "project" else None))
            return MigrationReport(scope, True, tuple(sorted(hosts)), backup if apply else None)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def replace_direct_with_relay(self, name: str, *, scope: Scope, identity: RelayRunHostIdentity, apply: bool) -> MigrationReport:
        self._require_posix_locking()
        path = self._path_for(scope)
        lock_fd = self._lock(path)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            hosts, policy = self._read_scope(scope)
            host = hosts.get(name)
            if host is None:
                raise RunHostError(f"run-host {name!r} is not configured in {scope} scope")
            if not isinstance(host.connection, RunHostConnection):
                raise RunHostError(f"run-host {name!r} is already relay-native")
            backup = path.with_name(path.name + ".relay.bak")
            if apply:
                self._atomic_write_bytes(backup, path.read_bytes())
                hosts[name] = replace(host, connection=identity)
                self._atomic_write(path, self._document(hosts, policy if scope == "project" else None))
            return MigrationReport(scope, True, (name,), backup if apply else None)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def safe_hosts(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for name, host in self.effective_hosts().items():
            safe: dict[str, object] = {"name": host.name, "roles": host.roles, "tags": host.tags, "preference": host.preference, "scope": host.scope}
            if isinstance(host.connection, RunHostConnection):
                safe.update({"mode": "direct", "url": host.connection.url})
            else:
                safe.update({"mode": "relay", "relay": host.connection.relay, "host_id": host.connection.host_id, "workspace_id": host.connection.workspace_id, "instance_id": host.connection.instance_id})
            result[name] = safe
        return result

    def connection_for_role(self, role: str, *, environ: Mapping[str, str]) -> RunHostConnection | None:
        """Legacy direct-only convenience; relay callers must use the resolver."""
        registration = _registration_for_role(self, role, environ=environ)
        if registration is None:
            return None
        if not isinstance(registration.connection, RunHostConnection):
            return None
        return registration.connection


def _registration_for_role(store: RunHostStore, role: str, *, environ: Mapping[str, str]) -> HostRegistration | None:
    hosts, policy = store.effective_hosts(), store.role_hosts()
    candidates = [host for host in hosts.values() if role in host.roles]
    if role in policy:
        candidates = [host for host in candidates if host.name in policy[role]]
    if len(candidates) > 1:
        raise RunHostError(f"ambiguous run-host role {role!r}; eligible hosts: {sorted(host.name for host in candidates)}")
    url, token = environ.get(_env_key(role, "URL")), environ.get(_env_key(role, "TOKEN"))
    if len(candidates) == 1:
        host = candidates[0]
        if not isinstance(host.connection, RunHostConnection) and (url or token):
            raise RunHostError("environment overrides are not permitted for relay run-host identities")
        if isinstance(host.connection, RunHostConnection) and (url or token):
            if not url or not token:
                raise RunHostError(f"direct run-host environment override for {role!r} requires both URL and TOKEN")
            return replace(host, connection=RunHostConnection(url, token))
        return host
    if role not in policy and url and token:
        return HostRegistration(role, (role,), (), 0, RunHostConnection(url, token), "project")
    return None


def resolve_run_host(role: str | None, *, repo: RepoConfig | None, config: WorkspaceConfig, store: RunHostStore) -> HostRegistration:
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
    collisions = [candidate for candidate in known if _env_key(candidate, "URL") == _env_key(chosen, "URL")]
    if len(collisions) > 1:
        raise RunHostError(f"ambiguous environment normalization for roles {sorted(collisions)}")
    try:
        host = _registration_for_role(store, chosen, environ=os.environ)
    except _MigrationRequired as exc:
        raise RunHostError(str(exc)) from exc
    if host is None:
        raise RunHostError(f"run-host role {chosen!r} is declared but has no eligible connection mapped on this machine; run `mship run-host add {chosen}` or migrate the legacy registry")
    return host
