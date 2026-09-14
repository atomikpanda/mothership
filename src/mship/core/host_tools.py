"""Server-owned, declaration-bounded mise readiness for remote run hosts.

This module deliberately does not locate a host, open a transport, or spawn a
process itself.  The caller supplies the already-authorized selected-host
identity and the existing supervised runner.  That keeps mise input discovery
and installation inside the materialized server worktree and on the one
existing `/exec/tool` path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


HostToolStatus = Literal[
    "unreachable",
    "unauthed",
    "unauthorized",
    "workspace_unavailable",
    "invalid_configuration",
    "missing_mise",
    "mismatched_tool",
    "missing_tool",
    "missing_sdk",
    "missing_path",
    "missing_license",
    "missing_config",
    "healthy",
]

_STATUS: frozenset[str] = frozenset(HostToolStatus.__args__)
_SAFE_RELATIVE = re.compile(r"^[^\\\x00*?\[\]{}]+$")
_SUPPORTED_MANIFESTS = frozenset(
    {
        "mise.toml",
        ".mise.toml",
        "mise/config.toml",
        ".mise/config.toml",
        ".config/mise.toml",
        ".config/mise/config.toml",
    }
)
_REQUIREMENT_KINDS: Mapping[str, str] = MappingProxyType(
    {
        "android-sdk": "sdk",
        "android-adb": "tool",
        "android-sdk-root": "path",
        "android-sdk-license": "license",
        "android-sdk-config": "config",
    }
)


def _validated_relative(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value or not _SAFE_RELATIVE.fullmatch(value):
        raise ValueError(f"host_tools {field} must be a normalized relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"host_tools {field} must stay inside the repository")
    return path.as_posix()


class HostToolMise(BaseModel):
    """The sole mise configuration root eligible for remote use."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    manifest: str
    lock: str | None = None

    @field_validator("manifest")
    @classmethod
    def validate_manifest(cls, value: str) -> str:
        return _validated_relative(value, field="mise.manifest")

    @field_validator("lock")
    @classmethod
    def validate_lock(cls, value: str | None) -> str | None:
        return None if value is None else _validated_relative(value, field="mise.lock")

    @model_validator(mode="after")
    def validate_manifest_layout(self) -> "HostToolMise":
        # Mise documents an override only for a root-level default filename.  A
        # nested arbitrary TOML path would be an unreviewed discovery rule.
        if self.manifest not in _SUPPORTED_MANIFESTS and len(Path(self.manifest).parts) != 1:
            raise ValueError("host_tools mise.manifest is not a documented mise layout")
        return self


class HostToolRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    kind: str

    @model_validator(mode="after")
    def validate_catalog_entry(self) -> "HostToolRequirement":
        expected = _REQUIREMENT_KINDS.get(self.id)
        if expected is None or self.kind != expected:
            raise ValueError("host_tools requirement is not in the supported catalog")
        return self


class HostToolsConfig(BaseModel):
    """Strict optional ``RepoConfig.host_tools`` declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    mise: HostToolMise
    requirements: tuple[HostToolRequirement, ...] = ()

    @field_validator("requirements")
    @classmethod
    def unique_requirements(
        cls, value: tuple[HostToolRequirement, ...]
    ) -> tuple[HostToolRequirement, ...]:
        if len({item.id for item in value}) != len(value):
            raise ValueError("host_tools requirements must not repeat an id")
        return value


@dataclass(frozen=True)
class HostToolIdentity:
    """Safe server-trusted identity; no caller role/name claim is accepted."""

    name: str
    role: str
    scope: Literal["user", "project", "server"]
    endpoint_fingerprint: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.name, self.role, self.endpoint_fingerprint)):
            raise ValueError("invalid host identity")


@dataclass(frozen=True)
class HostToolInvocation:
    """Bounded output returned by the existing supervised runner."""

    status: str
    exit_code: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass(frozen=True)
class HostToolCommand:
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    cwd: Path
    mutating: bool = False


@dataclass(frozen=True)
class HostToolResolution:
    identity: HostToolIdentity
    task: str
    repo: str
    source_revision: str
    declaration_digest: str | None
    manifest_digest: str | None
    lock_digest: str | None
    status: HostToolStatus
    requirements: tuple[tuple[str, str], ...] = ()
    tools: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.status not in _STATUS:
            raise ValueError("invalid host tool status")
        object.__setattr__(self, "requirements", tuple(self.requirements))
        object.__setattr__(self, "tools", tuple(self.tools))

    def safe_dict(self) -> dict[str, object]:
        return {
            "host": {
                "name": self.identity.name,
                "role": self.identity.role,
                "scope": self.identity.scope,
                "endpoint_fingerprint": self.identity.endpoint_fingerprint,
            },
            "task": self.task,
            "repo": self.repo,
            "source_revision": self.source_revision,
            "declaration_digest": self.declaration_digest,
            "manifest_digest": self.manifest_digest,
            "lock_digest": self.lock_digest,
            "status": self.status,
            "requirements": [dict(id=item, status=status) for item, status in self.requirements],
            "tools": [dict(name=name, version=version) for name, version in self.tools],
        }


@dataclass(frozen=True)
class HostToolDoctorReport:
    resolution: HostToolResolution
    category: HostToolStatus
    remediation: str

    def safe_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "remediation": self.remediation,
            "resolution": self.resolution.safe_dict(),
        }


@dataclass(frozen=True)
class _Inputs:
    manifest: Path
    lock: Path | None
    sidecars: tuple[Path, ...]
    declaration_digest: str
    manifest_digest: str
    lock_digest: str | None


class HostToolError(ValueError):
    """Safe configuration failure; deliberately contains no filesystem path."""


def _regular_relative(root: Path, relative: str) -> Path:
    candidate = root / relative
    try:
        resolved_root = root.resolve(strict=True)
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise HostToolError("declared host-tool input is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not resolved.is_relative_to(resolved_root):
        raise HostToolError("declared host-tool input is unsafe")
    return resolved


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _native_lock_path(manifest: str) -> str:
    """Documented native lockfile association for each accepted layout."""
    mapping = {
        "mise.toml": "mise.lock",
        ".mise.toml": "mise.lock",
        "mise/config.toml": "mise/mise.lock",
        ".mise/config.toml": ".mise/mise.lock",
        ".config/mise.toml": ".config/mise.lock",
        ".config/mise/config.toml": ".config/mise/mise.lock",
    }
    if manifest not in mapping:
        # A root-level MISE_DEFAULT_CONFIG_FILENAME still has the documented
        # root lock association; nested arbitrary names are rejected earlier.
        return "mise.lock"
    return mapping[manifest]


def _sidecar_paths(root: Path, lock: Path) -> tuple[Path, ...]:
    """Validate bounded lock-declared files and documented dependency directories."""
    try:
        document = tomllib.loads(lock.read_text("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise HostToolError("declared mise lock is invalid") from exc
    references: list[tuple[str, str]] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            path, digest = value.get("path"), value.get("digest")
            if isinstance(path, str) and isinstance(digest, str):
                references.append((path, digest))
            directory, files = value.get("directory"), value.get("files")
            if isinstance(directory, str) and isinstance(files, Mapping):
                if len(files) > 64:
                    raise HostToolError("declared mise sidecar directory is too large")
                for relative, expected in files.items():
                    if not isinstance(relative, str) or not isinstance(expected, str):
                        raise HostToolError("declared mise sidecar is invalid")
                    references.append((f"{directory}/{relative}", expected))
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(document)
    if len(references) > 128:
        raise HostToolError("declared mise sidecars are too large")
    result: list[Path] = []
    total_bytes = 0
    for relative, expected in references:
        try:
            candidate = _regular_relative(root, _validated_relative(relative, field="lock sidecar"))
        except ValueError as exc:
            raise HostToolError("declared mise sidecar is unsafe") from exc
        size = candidate.stat().st_size
        total_bytes += size
        if total_bytes > 1024 * 1024 or _sha256(candidate) != expected.removeprefix("sha256:"):
            raise HostToolError("declared mise sidecar does not match its lock")
        result.append(candidate)
    return tuple(sorted(set(result)))


def declaration_inputs(declaration: HostToolsConfig, worktree: Path) -> _Inputs:
    root = Path(worktree).resolve(strict=True)
    manifest = _regular_relative(root, declaration.mise.manifest)
    lock = None
    sidecars: tuple[Path, ...] = ()
    if declaration.mise.lock is not None:
        if declaration.mise.lock != _native_lock_path(declaration.mise.manifest):
            raise HostToolError("declared mise lock is not the active manifest lock")
        lock = _regular_relative(root, declaration.mise.lock)
        sidecars = _sidecar_paths(root, lock)
    digest = hashlib.sha256()
    canonical = declaration.model_dump(mode="json")
    digest.update(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode())
    for path in (manifest, *(sidecars), *((lock,) if lock is not None else ())):
        relative = path.relative_to(root).as_posix()
        digest.update(b"\0path:")
        digest.update(relative.encode())
        digest.update(b"\0bytes:")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return _Inputs(
        manifest=manifest,
        lock=lock,
        sidecars=sidecars,
        declaration_digest=digest.hexdigest(),
        manifest_digest=_sha256(manifest),
        lock_digest=None if lock is None else _sha256(lock),
    )


def mise_environment(
    *, worktree: Path, state_dir: Path, declaration: HostToolsConfig,
    locked: bool = False, safe: bool = True,
) -> dict[str, str]:
    """Server-owned configuration policy; host runtime/data remain available."""
    root = Path(worktree).resolve(strict=True)
    isolation = Path(state_dir) / "host-tools" / "mise-isolation"
    isolation.mkdir(parents=True, exist_ok=True, mode=0o700)
    global_config = isolation / "global.toml"
    global_config.touch(mode=0o600, exist_ok=True)
    system_dir = isolation / "system"
    system_dir.mkdir(mode=0o700, exist_ok=True)
    environment = {
        "MISE_CEILING_PATHS": str(root.parent),
        "MISE_GLOBAL_CONFIG_FILE": str(global_config),
        "MISE_SYSTEM_CONFIG_DIR": str(system_dir),
        "MISE_AUTO_ENV": "false",
        "MISE_SAFE": "1" if safe else "0",
        "MISE_AUTO_INSTALL": "false",
        "MISE_EXEC_AUTO_INSTALL": "false",
    }
    for name in ("MISE_DATA_DIR", "MISE_CACHE_DIR"):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    if declaration.mise.manifest not in _SUPPORTED_MANIFESTS:
        environment["MISE_DEFAULT_CONFIG_FILENAME"] = declaration.mise.manifest
    if locked:
        environment["MISE_LOCKED"] = "1"
    return environment


def _json_output(invocation: HostToolInvocation) -> object | None:
    if invocation.status != "completed" or invocation.exit_code != 0:
        return None
    try:
        return json.loads(invocation.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None


def _discovered_paths(value: object) -> set[str]:
    """Extract only filesystem-looking config source values from mise JSON."""
    found: set[str] = set()
    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if isinstance(nested, str) and (str(key).lower().endswith(("path", "file")) or nested.endswith(".toml")):
                    found.add(nested)
                else:
                    visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
    visit(value)
    return found


def _validate_config_output(value: object, *, inputs: _Inputs, environment: Mapping[str, str]) -> bool:
    paths = _discovered_paths(value)
    try:
        actual = {str(Path(path).resolve(strict=True)) for path in paths}
        allowed = {str(inputs.manifest), str(Path(environment["MISE_GLOBAL_CONFIG_FILE"]).resolve(strict=True))}
    except (OSError, KeyError):
        return False
    return actual == allowed


def _installed_tools(value: object, manifest: Path) -> tuple[bool, tuple[tuple[str, str], ...]]:
    try:
        declared = tomllib.loads(manifest.read_text("utf-8")).get("tools") or {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return True, ()
    if not isinstance(declared, Mapping) or not isinstance(value, Mapping):
        return True, ()
    result = []
    for name in declared:
        entries = value.get(name)
        entries = entries if isinstance(entries, list) else [entries]
        if not entries or any(not isinstance(item, Mapping) or item.get("installed") is not True for item in entries):
            return True, ()
        version = entries[0].get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            return True, ()
        result.append((name, version))
    return False, tuple(sorted(result))

def _android_sdk_root(
    run: Callable[[HostToolCommand], HostToolInvocation],
    *, environment: Mapping[str, str], worktree: Path
) -> Path | None:
    invocation = run(HostToolCommand(("android", "info", "sdk"), environment, worktree))
    if invocation.status != "completed" or invocation.exit_code != 0:
        return None
    try:
        root = Path(invocation.stdout.decode("utf-8").strip()).resolve(strict=True)
    except (OSError, UnicodeError):
        return None
    return root if root.is_dir() else None


def _status_for_requirement(requirement: HostToolRequirement, run: Callable[[HostToolCommand], HostToolInvocation], *, environment: Mapping[str, str], worktree: Path) -> str:
    if requirement.id == "android-sdk":
        check = run(HostToolCommand(("android", "sdk", "list"), environment, worktree))
        return "healthy" if check.status == "completed" and check.exit_code == 0 else "missing_sdk"
    if requirement.id == "android-adb":
        check = run(HostToolCommand(("adb", "version"), environment, worktree))
        return "healthy" if check.status == "completed" and check.exit_code == 0 else "missing_tool"
    root = _android_sdk_root(run, environment=environment, worktree=worktree)
    if root is None:
        return "missing_path"
    if requirement.id == "android-sdk-root":
        return "healthy"
    if requirement.id == "android-sdk-license":
        licenses = root / "licenses"
        return "healthy" if licenses.is_dir() and any(path.is_file() and path.stat().st_size for path in licenses.iterdir()) else "missing_license"
    check = run(HostToolCommand(("android", "info", "config"), environment, worktree))
    return "healthy" if check.status == "completed" and bool(check.stdout.strip()) else "missing_config"


def _category(requirements: tuple[tuple[str, str], ...]) -> HostToolStatus:
    statuses = {status for _, status in requirements}
    for candidate in ("missing_sdk", "missing_path", "missing_license", "missing_config", "missing_tool"):
        if candidate in statuses:
            return candidate  # type: ignore[return-value]
    return "healthy"


def diagnose(
    *, declaration: HostToolsConfig, worktree: Path, state_dir: Path,
    identity: HostToolIdentity, task: str, repo: str, source_revision: str,
    run: Callable[[HostToolCommand], HostToolInvocation],
) -> HostToolResolution:
    try:
        inputs = declaration_inputs(declaration, worktree)
    except (HostToolError, OSError, ValueError):
        return _resolution(identity, task, repo, source_revision, None, "invalid_configuration")
    env = mise_environment(worktree=worktree, state_dir=state_dir, declaration=declaration)
    version = run(HostToolCommand(("mise", "--version"), env, worktree))
    if version.status != "completed" or version.exit_code != 0:
        return _resolution(identity, task, repo, source_revision, inputs, "missing_mise")
    config = _json_output(run(HostToolCommand(("mise", "config", "--json"), env, worktree)))
    if config is None or not _validate_config_output(config, inputs=inputs, environment=env):
        return _resolution(identity, task, repo, source_revision, inputs, "invalid_configuration")
    current = _json_output(
        run(HostToolCommand(("mise", "ls", "--current", "--json"), env, worktree))
    )
    if current is None:
        return _resolution(identity, task, repo, source_revision, inputs, "mismatched_tool")
    missing, tools = _installed_tools(current, inputs.manifest)
    if missing:
        return _resolution(identity, task, repo, source_revision, inputs, "mismatched_tool", tools=tools)
    if inputs.lock is not None:
        locked = mise_environment(worktree=worktree, state_dir=state_dir, declaration=declaration, locked=True)
        dry = run(HostToolCommand(("mise", "install", "--dry-run"), locked, worktree))
        if dry.status != "completed" or dry.exit_code != 0:
            return _resolution(identity, task, repo, source_revision, inputs, "mismatched_tool", tools=tools)
        env = locked
    requirements = tuple((item.id, _status_for_requirement(item, run, environment=env, worktree=worktree)) for item in declaration.requirements)
    return _resolution(identity, task, repo, source_revision, inputs, _category(requirements), requirements, tools)


def _resolution(identity: HostToolIdentity, task: str, repo: str, source_revision: str, inputs: _Inputs | None, status: HostToolStatus, requirements: tuple[tuple[str, str], ...] = (), tools: tuple[tuple[str, str], ...] = ()) -> HostToolResolution:
    return HostToolResolution(identity, task, repo, source_revision, None if inputs is None else inputs.declaration_digest, None if inputs is None else inputs.manifest_digest, None if inputs is None else inputs.lock_digest, status, requirements, tools)


def remediation(status: HostToolStatus) -> str:
    return {
        "healthy": "host tools are ready",
        "missing_mise": "install mise on the selected run host, then rerun bootstrap --host-tools",
        "mismatched_tool": "run bootstrap --host-tools after reviewing the declared mise lock",
        "missing_tool": "install the declared tool through bootstrap --host-tools",
        "missing_sdk": "make the declared Android SDK available on the selected host",
        "missing_path": "configure the declared Android SDK root on the selected host",
        "missing_license": "complete the host's reviewed SDK licensing procedure outside mship",
        "missing_config": "configure the reviewed Android SDK setting on the selected host",
        "invalid_configuration": "fix the reviewed host_tools mise declaration and active configuration",
        "workspace_unavailable": "bootstrap the selected run-host workspace and retry",
        "unreachable": "check selected run-host connectivity and retry",
        "unauthed": "refresh the selected run-host credentials and retry",
        "unauthorized": "select an authorized run host and retry",
    }[status]


def doctor_report(resolution: HostToolResolution) -> HostToolDoctorReport:
    return HostToolDoctorReport(resolution, resolution.status, remediation(resolution.status))


def receipt_path(state_dir: Path, resolution: HostToolResolution) -> Path:
    identity = "\0".join((resolution.task, resolution.repo, resolution.identity.name, resolution.identity.endpoint_fingerprint, resolution.declaration_digest or ""))
    digest = hashlib.sha256(identity.encode()).hexdigest()
    readable = re.sub(r"[^A-Za-z0-9_-]", "-", f"{resolution.task}-{resolution.repo}")[:100]
    return Path(state_dir) / "host-tools" / f"{readable}-{digest[:24]}.json"


def receipt_matches(state_dir: Path, resolution: HostToolResolution) -> bool:
    """Receipts are only a durable success marker after a fresh full diagnosis."""
    try:
        payload = json.loads(receipt_path(state_dir, resolution).read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return payload == resolution.safe_dict()


def record_receipt(state_dir: Path, resolution: HostToolResolution) -> None:
    if resolution.status != "healthy" or resolution.declaration_digest is None:
        raise HostToolError("only healthy host-tool resolution may be recorded")
    target = receipt_path(state_dir, resolution)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=target.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(resolution.safe_dict(), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def bootstrap(
    *, declaration: HostToolsConfig, worktree: Path, state_dir: Path,
    identity: HostToolIdentity, task: str, repo: str, source_revision: str,
    run: Callable[[HostToolCommand], HostToolInvocation],
) -> HostToolResolution:
    initial = diagnose(declaration=declaration, worktree=worktree, state_dir=state_dir, identity=identity, task=task, repo=repo, source_revision=source_revision, run=run)
    if initial.status in {"invalid_configuration", "missing_mise"}:
        return initial
    if initial.status == "healthy":
        if not receipt_matches(state_dir, initial):
            record_receipt(state_dir, initial)
        return initial
    env = mise_environment(worktree=worktree, state_dir=state_dir, declaration=declaration, locked=declaration.mise.lock is not None, safe=False)
    installation = run(HostToolCommand(("mise", "install"), env, worktree, mutating=True))
    if installation.status != "completed" or installation.exit_code != 0:
        return initial
    resolution = diagnose(declaration=declaration, worktree=worktree, state_dir=state_dir, identity=identity, task=task, repo=repo, source_revision=source_revision, run=run)
    if resolution.status == "healthy" and not receipt_matches(state_dir, resolution):
        record_receipt(state_dir, resolution)
    return resolution


def server_identity(state_dir: Path) -> HostToolIdentity:
    """Persist a server-created opaque host identity; never trust request fields."""
    path = Path(state_dir) / "host-tools" / "server-identity.json"
    try:
        payload = json.loads(path.read_text("utf-8"))
        if all(isinstance(payload.get(key), str) and payload[key] for key in ("name", "fingerprint")):
            return HostToolIdentity(payload["name"], "selected", "server", payload["fingerprint"])
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = {"name": "run-host", "fingerprint": hashlib.sha256(secrets.token_bytes(32)).hexdigest()}
    descriptor, temporary = tempfile.mkstemp(prefix=".identity-", dir=path.parent)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return HostToolIdentity(value["name"], "selected", "server", value["fingerprint"])



def remote_operation(
    *,
    action: Literal["diagnose", "bootstrap"],
    task_obj: object,
    repo: str,
    config: object,
    shell: object,
    host: object,
    resolver: object,
    output: object,
    event_sink: Callable[[object], None] | None = None,
) -> HostToolDoctorReport | None:
    """Dispatch one scoped host-tools request over the existing authenticated path."""
    from mship.core.remote_dispatch import run_remote_tool
    from mship.core.remote_tool import ToolRequest
    from mship.core.run_host import registration_identity

    task = getattr(task_obj, "slug", None)
    if not isinstance(task, str):
        return None
    request = ToolRequest(
        task=task,
        repo=repo,
        argv=(),
        host_tools_action=action,
        preparation="discover" if action == "diagnose" else "launch",
        max_stdout_bytes=64 * 1024 if action == "diagnose" else None,
        max_stderr_bytes=16 * 1024 if action == "diagnose" else None,
        timeout_seconds=30 if action == "diagnose" else None,
    )
    result = run_remote_tool(
        request=request,
        task_obj=task_obj,
        config=config,
        shell=shell,
        host=host,
        resolver=resolver,
        output=output,
        event_sink=event_sink,
    )
    fingerprint = hashlib.sha256(
        "|".join(registration_identity(host.connection)).encode()
    ).hexdigest()
    identity = HostToolIdentity(
        host.name, host.roles[0] if host.roles else "selected", host.scope, fingerprint
    )
    raw = getattr(result, "host_tools_report", None)
    if not isinstance(raw, Mapping):
        status = {
            "auth_error": "unauthed",
            "materialization_error": "workspace_unavailable",
            "protocol_error": "unreachable",
            "unreachable": "unreachable",
            "unauthed": "unauthed",
            "unauthorized": "unauthorized",
            "workspace_unavailable": "workspace_unavailable",
        }.get(getattr(result, "status", ""), "unreachable")
        resolution = HostToolResolution(
            identity,
            task,
            repo,
            "",
            None,
            None,
            None,
            status,
        )
        return doctor_report(resolution)
    try:
        resolution_data = raw["resolution"]
        if resolution_data["task"] != task or resolution_data["repo"] != repo:
            return None
        requirements = tuple(
            (item["id"], item["status"]) for item in resolution_data.get("requirements", [])
        )
        tools = tuple(
            (item["name"], item["version"]) for item in resolution_data.get("tools", [])
        )
        resolution = HostToolResolution(
            identity,
            resolution_data["task"],
            resolution_data["repo"],
            resolution_data["source_revision"],
            resolution_data.get("declaration_digest"),
            resolution_data.get("manifest_digest"),
            resolution_data.get("lock_digest"),
            resolution_data["status"],
            requirements,
            tools,
        )
        return doctor_report(resolution)
    except (KeyError, TypeError, ValueError):
        return None
