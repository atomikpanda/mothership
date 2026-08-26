from __future__ import annotations

from collections.abc import Mapping
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Literal

import yaml
from pydantic import ValidationError

from mship.core.inbox import InboxAction, apply_inbox_action


from mship.core.spec import Spec

SPECS_DIRNAME = "specs"  # canonical name of the workspace-level specs directory


@contextmanager
def _locked(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


class SpecParseError(Exception):
    pass


class SpecArtifactConflict(SpecParseError):
    """A spec id does not map to exactly one trustworthy on-disk artifact."""


class SpecRepresentationMismatch(SpecArtifactConflict):
    """An existing artifact would require changing representation outside migration."""


@dataclass(frozen=True)
class ResolvedSpecArtifact:
    """The sole authoritative on-disk artifact for one logical spec id."""

    spec: Spec
    logical_path: Path
    physical_path: Path
    representation: Literal["plaintext", "encrypted"]
    policy: Literal["committed", "local", "encrypted"]


# MOS-240: legacy spec statuses (captured/drafting/needs_clarification) are mapped
# forward by the Spec model's `_migrate_legacy_status` validator (see core/spec.py),
# so EVERY construction path — parse_spec, `Spec.model_validate_json`, direct
# construction — handles old serialized data, not just this reader.
def parse_spec(text: str) -> Spec:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SpecParseError("spec file missing YAML frontmatter")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise SpecParseError("unterminated YAML frontmatter")
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1:])
    try:
        data = yaml.safe_load(fm_text) or {}
        if not isinstance(data, Mapping):
            raise SpecParseError("spec frontmatter must be a mapping")
        return Spec(**data, body=body)
    except yaml.YAMLError as exc:
        raise SpecParseError(f"invalid YAML frontmatter: {exc}") from exc
    except (TypeError, ValidationError) as exc:
        raise SpecParseError(f"spec frontmatter failed validation: {exc}") from exc


def serialize_spec(spec: Spec) -> str:
    data = spec.model_dump(mode="json", exclude={"body"})
    fm = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    return f"---\n{fm}---\n{spec.body}"


class SpecStore:
    """Filesystem registry for markdown-canonical specs under `specs/`.

    All on-disk representation (plaintext vs Fernet ciphertext, filename suffix,
    gitignore) is delegated to a `SpecStorage` (spec-storage-visibility-policy).
    When no `storage` is passed the mode is resolved from the workspace
    `spec_storage` config (`storage_from_workspace`) — so EVERY construction site
    (the spec CLI verbs, serve, the lifecycle persisters, `cli/worktree.py`) is
    mode-correct by construction: a writer under an encrypted workspace can never
    accidentally emit plaintext. A workspace with no config defaults to committed
    — today's behaviour — so existing call sites and tests are unchanged.
    """

    def __init__(self, specs_dir: Path, storage=None) -> None:
        self._dir = Path(specs_dir)
        if storage is None:
            from mship.core.spec_storage import storage_from_workspace
            storage = storage_from_workspace(self._dir)
        self._storage = storage

    @property
    def workspace_root(self) -> Path:
        """The workspace root this store's specs live under (`<root>/specs`).
        Reuses `SpecStorage`'s already-computed root — the single place that
        derives it — so callers needing e.g. `<root>/.mothership/logs`
        (view/actions.py's LogManager) don't restate the convention."""
        return self._storage.workspace_root

    def _validate_id(self, spec_id: str) -> None:
        if (not spec_id or "/" in spec_id or "\\" in spec_id
                or spec_id in (".", "..") or spec_id.startswith(".")):
            raise ValueError(f"unsafe spec id for filename: {spec_id!r}")

    def _lock_path(self, spec_id: str) -> Path:
        """Per-spec runtime lock, outside storage-mode-managed spec artifacts."""
        self._validate_id(spec_id)
        return self.workspace_root / ".mothership" / "locks" / "specs" / f"{spec_id}.lock"

    def path_for(self, spec: Spec) -> Path:
        """Logical `.md` stem for a spec: `<specs_dir>/<created_at date>-<id>.md`.

        The physical filename (e.g. `.md.enc` under encrypted mode) is resolved by
        the storage layer at write time. Saving again with the same id + creation
        date overwrites the file (this is the intended update mechanism).
        """
        self._validate_id(spec.id)
        return self._dir / f"{spec.created_at:%Y-%m-%d}-{spec.id}.md"

    @contextmanager
    def locked(self, spec_id: str) -> Iterator[ResolvedSpecArtifact | None]:
        """Hold the per-spec lock while resolving and mutating its physical artifact."""
        with _locked(self._lock_path(spec_id)):
            yield self.resolve_artifact(spec_id)

    def _logical_path(self, physical_path: Path) -> Path:
        if physical_path.name.endswith(".enc"):
            return physical_path.with_name(physical_path.name[:-4])
        return physical_path

    def resolve_artifact(self, spec_id: str) -> ResolvedSpecArtifact | None:
        """Resolve exactly one parsed artifact for ``spec_id`` or fail closed."""
        from mship.core.spec_storage import (
            SpecLocked, canonical_spec_id_from_filename,
        )

        self._validate_id(spec_id)
        matches: list[tuple[Spec, Path]] = []
        exact_locked: SpecLocked | None = None
        locked_renamed: Path | None = None
        for physical_path in self._storage.iter_physical():
            canonical_id = canonical_spec_id_from_filename(physical_path)
            try:
                parsed = parse_spec(self._storage.decode_file(physical_path))
            except SpecLocked as locked:
                if canonical_id == spec_id:
                    exact_locked = locked
                elif canonical_id is None:
                    locked_renamed = physical_path
                continue
            except SpecParseError as exc:
                if canonical_id == spec_id:
                    raise
                if canonical_id is None:
                    raise SpecArtifactConflict(
                        f"spec store has an unreadable renamed artifact: {physical_path}"
                    ) from exc
                continue

            if canonical_id is not None and parsed.id != canonical_id:
                if spec_id in (canonical_id, parsed.id):
                    raise SpecArtifactConflict(
                        f"canonical artifact {physical_path} binds {canonical_id!r}, "
                        f"not frontmatter id {parsed.id!r}"
                    )
                continue
            if parsed.id == spec_id:
                matches.append((parsed, physical_path))

        if locked_renamed is not None:
            raise SpecArtifactConflict(
                f"spec id {spec_id!r} has an unreadable renamed artifact: {locked_renamed}"
            )
        if exact_locked is not None:
            if matches:
                raise SpecArtifactConflict(
                    f"spec id {spec_id!r} has both locked and readable artifacts"
                )
            raise exact_locked
        if len(matches) > 1:
            paths = ", ".join(str(path) for _, path in matches)
            raise SpecArtifactConflict(
                f"spec id {spec_id!r} has multiple physical artifacts: {paths}"
            )
        if not matches:
            return None
        spec, physical_path = matches[0]
        encrypted = physical_path.name.endswith(".enc")
        return ResolvedSpecArtifact(
            spec=spec,
            logical_path=self._logical_path(physical_path),
            physical_path=physical_path,
            representation="encrypted" if encrypted else "plaintext",
            policy="encrypted" if encrypted else self._storage.mode,
        )

    def _save_unlocked(
        self, spec: Spec, artifact: ResolvedSpecArtifact | None = None,
    ) -> Path:
        """Persist under the caller-held lock without replacing the selected artifact."""
        artifact = artifact if artifact is not None else self.resolve_artifact(spec.id)
        target_representation = "encrypted" if self._storage.mode == "encrypted" else "plaintext"
        if artifact is not None:
            if artifact.representation != target_representation:
                raise SpecRepresentationMismatch(
                    f"spec {spec.id!r} is {artifact.representation} at "
                    f"{artifact.physical_path}; migrate storage before writing it as "
                    f"{target_representation}"
                )
            return self._storage.write(artifact.logical_path, serialize_spec(spec))
        return self._storage.write(self.path_for(spec), serialize_spec(spec))

    def save_migrated_unlocked(self, spec: Spec) -> Path:
        """Write the configured representation while ``locked(spec.id)`` is held."""
        return self._storage.write(self.path_for(spec), serialize_spec(spec))

    def save(self, spec: Spec) -> Path:
        """Persist lifecycle changes without clobbering durable inbox metadata."""
        with self.locked(spec.id) as artifact:
            if artifact is not None:
                spec.inbox = artifact.spec.inbox
            return self._save_unlocked(spec, artifact)

    def create_if_absent(self, spec: Spec) -> Path | None:
        """Atomically create a spec and return its actual physical path."""
        with self.locked(spec.id) as artifact:
            if artifact is not None:
                return None
            return self._save_unlocked(spec)

    def load(self, path: Path) -> Spec:
        return parse_spec(self._storage.decode_file(Path(path)))

    def list(self) -> list[Spec]:
        """Strictly list artifacts for workflow gates.

        Locked, corrupt, conflicting, or duplicate artifacts fail closed. Display
        callers that must preserve readable siblings use ``list_tolerant``.
        """
        from mship.core.spec_storage import SpecLocked, canonical_spec_id_from_filename

        specs: list[Spec] = []
        paths_by_id: dict[str, Path] = {}
        for path in self._storage.iter_physical():
            canonical_id = canonical_spec_id_from_filename(path)
            try:
                spec = parse_spec(self._storage.decode_file(path))
            except SpecLocked:
                raise
            if canonical_id is not None and spec.id != canonical_id:
                raise SpecArtifactConflict(
                    f"canonical artifact {path} binds {canonical_id!r}, "
                    f"not frontmatter id {spec.id!r}"
                )
            existing_path = paths_by_id.get(spec.id)
            if existing_path is not None:
                raise SpecArtifactConflict(
                    f"spec id {spec.id!r} has multiple physical artifacts: "
                    f"{existing_path}, {path}"
                )
            paths_by_id[spec.id] = path
            specs.append(spec)
        return specs

    def list_tolerant(self) -> list[Spec]:
        """List readable, identity-valid specs while omitting unavailable artifacts."""
        return [
            spec
            for spec, _locked_id, _path in self._storage.read_all()
            if isinstance(spec, Spec)
        ]

    def find_by_id(self, spec_id: str) -> Spec | None:
        """Tolerant compatibility lookup: locked artifacts are unavailable."""
        from mship.core.spec_storage import SpecLocked

        try:
            artifact = self.resolve_artifact(spec_id)
        except SpecLocked:
            return None
        return artifact.spec if artifact is not None else None

    def read_strict(self, spec_id: str) -> Spec | None:
        """Strictly resolve one physical artifact for an id."""
        artifact = self.resolve_artifact(spec_id)
        return artifact.spec if artifact is not None else None

    def mutate_inbox(
        self,
        spec_id: str,
        action: InboxAction,
        mutation_id: str,
        now: datetime,
    ) -> tuple[Spec, bool]:
        """Apply an inbox action and report whether it changed inbox state."""
        with self.locked(spec_id) as artifact:
            if artifact is None:
                raise KeyError(spec_id)
            spec = artifact.spec
            result = apply_inbox_action(spec.inbox, action, mutation_id, now)
            if result.persisted:
                self._save_unlocked(spec, artifact)
            return spec, result.applied
