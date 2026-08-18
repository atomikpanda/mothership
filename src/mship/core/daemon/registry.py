"""Workspace registry (#472): the daemon's durable, addressable view of the
workspaces discovered on this host, plus the daemon's own scan configuration.

Identity: `WorkspaceEntry.id` is minted (`ws-<ts>-<uuid8>`) and NEVER derived
from the directory name — two same-basename (even same display-name)
workspaces must coexist, and a rename must not re-identify a workspace.

Cross-host semantics: a registry is local to one host daemon. The same
workspace directory registered in two registries (two `home`s) is two
independent entries by design; NOTHING here implies an exclusive execution
owner — cross-host arbitration of who runs a WorkItem belongs to #473's
claim protocol, not to registry state.

Store: single-doc flock'd RMW (the `state.py::_locked` house pattern — each
per-file store carries its own copy, per workitem_store.py:20). Registry
writes are host-local and low-frequency; one lock suffices.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, field_validator


class RepoInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    path: str
    git_root: Optional[str] = None  # monorepo membership (parent repo name)


class RuntimeInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    interpreter: Optional[str] = None  # never the daemon's own sys.executable
    venv_path: Optional[str] = None


class WorkspaceEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    path: str
    config_path: str
    state: Literal["healthy", "degraded", "missing"] = "healthy"
    detail: str = ""
    repos: list[RepoInfo] = []
    runtime: RuntimeInfo = RuntimeInfo()
    # Opaque #473 passthrough: the raw `runner:` yaml block. Schema semantics
    # land with #473; the registry only guarantees it resolves from the entry.
    runner: Optional[dict] = None
    identity_source: Literal["idfile", "registry-only"] = "idfile"
    origin: Literal["discovered", "manual"] = "discovered"
    ignored: bool = False
    first_seen: datetime
    last_seen: datetime
    # Git identity is deliberately NOT a field: with cwd explicit on every
    # daemon path, git resolves user.name/email from each repo's local config.
    # A recorded identity field can land with #473 if worker env needs it.


class RegistryState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    entries: list[WorkspaceEntry] = []


def mint_workspace_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"ws-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


@contextmanager
def _locked(lock_path: Path, mode: int):
    """Advisory flock (mirrors state.py's `_locked`)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path.parent.chmod(0o700)
    lock_path.touch(exist_ok=True, mode=0o600)
    lock_path.chmod(0o600)
    with open(lock_path, "r+") as lf:
        fcntl.flock(lf, mode)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


class RegistryReadError(RuntimeError):
    """An existing registry could not be read from durable storage."""


class RegistryStore:
    """Flock'd RMW over one JSON doc; `mutate` spans the whole read-modify-write."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = path.with_name(path.name + ".lock")

    def load(self) -> RegistryState:
        with _locked(self._lock, fcntl.LOCK_SH):
            return self._load_nolock()

    def _load_nolock(self) -> RegistryState:
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return RegistryState()
        except OSError as exc:
            raise RegistryReadError(
                f"cannot read workspace registry {self._path}: {exc}"
            ) from exc
        try:
            return RegistryState.model_validate(json.loads(raw))
        except ValueError:
            return RegistryState()

    def _save_nolock(self, state: RegistryState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(state.model_dump_json(indent=2))
        tmp.replace(self._path)

    def mutate(self, fn: Callable[[RegistryState], RegistryState | None]) -> RegistryState:
        """Exclusive-locked read-modify-write. `fn` may mutate in place (return
        None) or return a replacement state."""
        with _locked(self._lock, fcntl.LOCK_EX):
            state = self._load_nolock()
            result = fn(state)
            state = result if result is not None else state
            self._save_nolock(state)
            return state


class DaemonConfig(BaseModel):
    """The daemon's own scan/serve/relay configuration (`~/.mothership/daemon/config.yaml`).

    Missing file → empty scan roots (scan NOTHING — bounded by construction),
    no TCP bind (control-UDS only) and no relay tunnel."""

    model_config = ConfigDict(extra="ignore")
    scan_roots: list[str] = []
    ignore_globs: list[str] = []
    max_depth: int = 6
    serve: Optional[dict] = None  # {"host": str, "port": int}
    relay: Optional[dict] = None  # {"host": str, "ssh_port": int?, "user": str?}

    @field_validator("max_depth")
    @classmethod
    def _validate_max_depth(cls, value: int) -> int:
        if value < 0:
            raise ValueError("daemon max_depth must be nonnegative")
        return value


    @field_validator("serve", mode="before")
    @classmethod
    def _validate_serve(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("daemon serve must be a host/port mapping")
        host = value.get("host")
        port = value.get("port")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("daemon serve.host must be a non-empty string")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("daemon serve.port must be an integer from 1 to 65535")
        return {"host": host, "port": port}

    @field_validator("relay", mode="before")
    @classmethod
    def _validate_relay(cls, value):
        """Validated THROUGH `RelayConfig.from_mapping` — the tunnel's own
        constructor — so the config boundary and the dialer can never disagree
        about what a usable relay block is. An empty block is rejected rather
        than read as "no relay": `from_mapping` treats falsy input as absent,
        which is right for a caller that never wrote one and wrong for an
        operator who wrote `relay:` and forgot the host."""
        from mship.core.relay.config import RELAY_HOST_REQUIRED, RelayConfig

        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("daemon relay must be a host/ssh_port/user mapping")
        if not value:
            raise ValueError(RELAY_HOST_REQUIRED)
        RelayConfig.from_mapping(value)
        return value


class DaemonConfigReadError(ValueError):
    """Existing daemon config could not be read; never equivalent to invalid."""


def load_daemon_config(home: Path) -> DaemonConfig:
    from mship.core.daemon.paths import daemon_config_path

    path = daemon_config_path(home)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        return DaemonConfig()
    except OSError as exc:
        raise DaemonConfigReadError(
            f"cannot read daemon config {path}: {exc}"
        ) from exc
    except yaml.YAMLError as e:
        raise ValueError(f"invalid daemon config {path}: {e}") from e
    cfg = DaemonConfig.model_validate(raw)
    bad = [r for r in cfg.scan_roots if not os.path.isabs(r)]
    if bad:
        raise ValueError(f"daemon config scan_roots must be absolute paths, got: {bad}")
    return cfg


def save_daemon_config(home: Path, cfg: DaemonConfig) -> None:
    from mship.core.daemon.paths import daemon_config_path
    path = daemon_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    content = yaml.safe_dump(
        cfg.model_dump(exclude_none=True), sort_keys=False
    ).encode()
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", dir=path.parent
    )
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "wb")
        fd = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Reconciliation (#472 Task 4): scan candidates -> registry entries.
# ---------------------------------------------------------------------------

ID_FILE_RELPATH = Path(".mothership") / "workspace-id"


def _read_id_file(ws_path: Path) -> str | None:
    root_fd = owner_fd = file_fd = None
    try:
        root_fd = os.open(
            ws_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        owner_fd = os.open(
            ".mothership",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        file_fd = os.open(
            "workspace-id",
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=owner_fd,
        )
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            return None
        with os.fdopen(file_fd) as stream:
            file_fd = None
            value = stream.read().strip()
        return value or None
    except OSError:
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if owner_fd is not None:
            os.close(owner_fd)
        if root_fd is not None:
            os.close(root_fd)


def _write_id_file(ws_path: Path, workspace_id: str) -> bool:
    root_fd = owner_fd = None
    temp_name = f".workspace-id.{uuid.uuid4().hex}.tmp"
    try:
        root_fd = os.open(
            ws_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.mkdir(".mothership", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        owner_fd = os.open(
            ".mothership",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            target = os.stat(
                "workspace-id", dir_fd=owner_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            target = None
        if target is not None and not stat.S_ISREG(target.st_mode):
            return False
        fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=owner_fd,
        )
        with os.fdopen(fd, "w") as stream:
            stream.write(workspace_id + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temp_name,
            "workspace-id",
            src_dir_fd=owner_fd,
            dst_dir_fd=owner_fd,
        )
        return True
    except OSError:
        return False
    finally:
        if owner_fd is not None:
            try:
                os.unlink(temp_name, dir_fd=owner_fd)
            except OSError:
                pass
            os.close(owner_fd)
        if root_fd is not None:
            os.close(root_fd)


def _recover_registry_only_identity(
    entry: WorkspaceEntry,
    workspace_path: Path,
    file_id: str | None,
    by_id: dict[str, WorkspaceEntry],
) -> bool:
    """Repair a healthy registry-only entry; false means a known ID collision."""
    if entry.identity_source != "registry-only":
        return True
    if file_id is not None:
        owner = by_id.get(file_id)
        if owner is not None and owner is not entry:
            return False
        if by_id.get(entry.id) is entry:
            by_id.pop(entry.id)
        entry.id = file_id
        entry.identity_source = "idfile"
        by_id[file_id] = entry
    elif _write_id_file(workspace_path, entry.id):
        entry.identity_source = "idfile"
    return True


def _active_path_owner(
    entries: list[WorkspaceEntry], path: str
) -> WorkspaceEntry | None:
    """Current path ownership excludes retained missing identity history."""
    return next(
        (
            entry
            for entry in reversed(entries)
            if entry.path == path and entry.state != "missing"
        ),
        None,
    )


def _displace_conflicting_path_owner(
    entries: list[WorkspaceEntry],
    path: str,
    authoritative_id: str,
) -> WorkspaceEntry | None:
    """Durable ID wins a path unless its owner is a deliberate duplicate."""
    owner = _active_path_owner(entries, path)
    if (
        owner is not None
        and owner.id != authoritative_id
        and "duplicate-identity" not in owner.detail
    ):
        owner.state = "missing"
        owner.detail = "workspace was replaced at this path"
        return None
    return owner


def reconcile(store: RegistryStore, candidates: list, now: datetime) -> RegistryState:
    """One flock'd RMW folding a scan's candidates into the registry.

    Identity rules (#472):
    - healthy newcomers mint an id and persist it to <ws>/.mothership/workspace-id
      (degraded candidates get NO id file written into their directory);
    - a candidate whose id file matches an existing entry is that entry MOVED
      (path updated, overrides like `ignored` preserved);
    - duplicate identity (same id at two live paths — cp -r backup, cloned
      image): the entry KEEPS its current path; the copy surfaces as a visible
      degraded `duplicate-identity` entry with no id rewrite, independent of
      candidate iteration order;
    - a registered path no longer discovered and no longer existing degrades to
      `missing` (still listed — never a phantom, never a dispatch-time crash);
    - two entries resolving to the same state dir: the later one degrades
      visibly (state-dir collision backstop).
    """

    def apply(state: RegistryState) -> None:
        by_id = {e.id: e for e in state.entries}
        by_path = {
            entry.path: entry
            for entry in state.entries
            if entry.state != "missing"
        }
        round_candidates = list(candidates)
        candidate_paths = {str(cand.path) for cand in round_candidates}
        manual_entries = [
            entry
            for entry in state.entries
            if entry.origin == "manual"
            and entry.path not in candidate_paths
            and Path(entry.path).exists()
            and (
                entry.state != "missing"
                or _read_id_file(Path(entry.path)) == entry.id
            )
        ]
        if manual_entries:
            from mship.core.daemon.discovery import (
                _is_task_worktree,
                _materialize,
            )

            for entry in manual_entries:
                path = Path(entry.path)
                if _is_task_worktree(path):
                    entry.state = "degraded"
                    entry.detail = "task worktree is not an independent workspace"
                    entry.last_seen = now
                    continue
                round_candidates.append(_materialize(path))

        seen_paths: set[str] = set()
        # Resolve known identities before path-only newcomers. This makes a
        # move vacate its old path before a new workspace there is matched.
        prepared = [
            (cand, _read_id_file(cand.path)) for cand in round_candidates
        ]
        ordered = sorted(
            prepared,
            key=lambda item: (
                0 if item[1] is not None and item[1] in by_id else 1,
                str(item[0].path),
            ),
        )
        claimed_ids: dict[str, str] = {}  # id -> path that holds it this round
        for cand, file_id in ordered:
            path_str = str(cand.path)
            seen_paths.add(path_str)
            existing = None
            if file_id is not None and file_id in by_id:
                existing = by_id[file_id]
            elif path_str in by_path:
                existing = by_path[path_str]

            if (
                existing is not None
                and cand.healthy
                and file_id is None
                and existing.identity_source == "idfile"
            ):
                # The durable identity vanished while a healthy workspace now
                # occupies the same path. Preserve the old identity as history;
                # registry-only entries are the recoverable no-id-file case.
                existing.state = "missing"
                existing.detail = "workspace was replaced at this path"
                if by_path.get(path_str) is existing:
                    by_path.pop(path_str)
                existing = None

            if file_id is not None and file_id in claimed_ids and claimed_ids[file_id] != path_str:
                # Duplicate identity within one scan round: keeper decided below
                # by current-path rule; both orders converge because the entry's
                # recorded path wins, not iteration order.
                pass

            if existing is not None and file_id == existing.id and existing.path != path_str:
                # Once a copy has its own degraded registry entry, it remains a
                # duplicate until `workspace add` re-identifies it — even if
                # the original disappears. Otherwise the original is moved
                # here and two entries wind up claiming this path.
                dup = _displace_conflicting_path_owner(
                    state.entries, path_str, existing.id
                )
                if dup is None:
                    by_path.pop(path_str, None)
                if dup is not None:
                    dup.state = "degraded"
                    dup.detail = (
                        f"duplicate-identity: id file matches {existing.path} ({existing.id}); "
                        "`mship workspace add` this copy to mint a fresh id"
                    )
                    dup.last_seen = now
                    continue
                other_alive = Path(existing.path).exists() and _read_id_file(Path(existing.path)) == existing.id
                if other_alive:
                    # COPY: keep the entry's current path; this path degrades.
                    state.entries.append(WorkspaceEntry(
                        id=mint_workspace_id(now), name=cand.name or cand.path.name,
                        path=path_str, config_path=str(cand.config_path),
                        state="degraded",
                        detail=f"duplicate-identity: id file matches {existing.path} ({existing.id}); `mship workspace add` this copy to mint a fresh id",
                        identity_source="registry-only", first_seen=now, last_seen=now,
                    ))
                    continue
                # MOVE: same id, old path gone -> update path, preserve overrides.
                old_path = existing.path
                existing.path = path_str
                existing.config_path = str(cand.config_path)
                if by_path.get(old_path) is existing:
                    by_path.pop(old_path)
                by_path[path_str] = existing

            if existing is None:
                if not cand.healthy:
                    state.entries.append(WorkspaceEntry(
                        id=mint_workspace_id(now), name=cand.name or cand.path.name,
                        path=path_str, config_path=str(cand.config_path),
                        state="degraded", detail=cand.detail,
                        identity_source="registry-only", first_seen=now, last_seen=now,
                        repos=cand.repos, runtime=cand.runtime, runner=cand.runner,
                    ))
                    continue
                new_id = file_id or mint_workspace_id(now)
                wrote = True if file_id else _write_id_file(cand.path, new_id)
                entry = WorkspaceEntry(
                    id=new_id, name=cand.name or cand.path.name, path=path_str,
                    config_path=str(cand.config_path),
                    identity_source="idfile" if wrote else "registry-only",
                    repos=cand.repos, runtime=cand.runtime, runner=cand.runner,
                    first_seen=now, last_seen=now,
                )
                state.entries.append(entry)
                by_id[entry.id] = entry
                by_path[path_str] = entry
                claimed_ids[entry.id] = path_str
                continue

            if cand.healthy:
                _recover_registry_only_identity(
                    existing, cand.path, file_id, by_id
                )
            # Refresh an existing entry in place.
            existing.last_seen = now
            claimed_ids[existing.id] = existing.path
            if cand.healthy:
                existing.state = "healthy"
                existing.detail = ""
                existing.name = cand.name or existing.name
                existing.repos = cand.repos
                existing.runtime = cand.runtime
                existing.runner = cand.runner
            else:
                existing.state = "degraded"
                existing.detail = cand.detail

        # Anything registered but not seen this round.
        for e in state.entries:
            path_exists = Path(e.path).exists()
            if e.path in seen_paths or e.origin == "manual" and path_exists:
                continue
            if not path_exists:
                e.state = "missing"
                e.detail = "workspace path no longer exists"
            elif e.origin == "discovered":
                e.state = "degraded"
                e.detail = "workspace no longer under configured scan roots"

        # State-dir collision backstop: two entries, one resolved state dir.
        from mship.core.workspace_context import _resolve_state_dir

        state_dirs: dict[str, str] = {}
        for e in state.entries:
            if e.state != "healthy":
                continue
            sd = str(_resolve_state_dir(Path(e.config_path)))
            if sd in state_dirs:
                e.state = "degraded"
                e.detail = f"state-dir collision with {state_dirs[sd]}"
            else:
                state_dirs[sd] = e.id

    return store.mutate(apply)
