from __future__ import annotations

import fcntl
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

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
        return Spec(**data, body=body)
    except yaml.YAMLError as exc:
        raise SpecParseError(f"invalid YAML frontmatter: {exc}") from exc
    except ValidationError as exc:
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
        """Per-spec lock that is independent of the storage mode's physical path."""
        self._validate_id(spec_id)
        return self._dir / f".{spec_id}.inbox.lock"

    def path_for(self, spec: Spec) -> Path:
        """Logical `.md` stem for a spec: `<specs_dir>/<created_at date>-<id>.md`.

        The physical filename (e.g. `.md.enc` under encrypted mode) is resolved by
        the storage layer at write time. Saving again with the same id + creation
        date overwrites the file (this is the intended update mechanism).
        """
        self._validate_id(spec.id)
        return self._dir / f"{spec.created_at:%Y-%m-%d}-{spec.id}.md"

    def save(self, spec: Spec) -> Path:
        return self._storage.write(self.path_for(spec), serialize_spec(spec))

    def load(self, path: Path) -> Spec:
        return parse_spec(self._storage.decode_file(Path(path)))

    def list(self) -> list[Spec]:
        # Skip LOCKED specs (encrypted, no key — an expected, graceful state) so one
        # un-decryptable file doesn't block the readable siblings or every routed CLI
        # op via find_by_id (Greptile #402 "one locked file blocks all"). But do NOT
        # swallow a corrupt/unreadable store: a malformed spec PROPAGATES so it can't
        # silently vanish from the list — which would let resolve_bound_spec return
        # None and finish --require-evidence skip a required check (Greptile #341).
        # (serve's LOCKED-aware display uses the fully-tolerant read_all; the gate's
        # list must fail safe on corruption, so it only tolerates the locked case.)
        from mship.core.spec_storage import SpecLocked
        specs: list[Spec] = []
        for path in self._storage.iter_physical():
            try:
                text = self._storage.decode_file(path)
            except SpecLocked:
                continue
            specs.append(parse_spec(text))   # SpecParseError / read errors propagate
        return specs

    def find_by_id(self, spec_id: str) -> Spec | None:
        for spec in self.list():
            if spec.id == spec_id:
                return spec
        return None

    def read_strict(self, spec_id: str) -> Spec | None:
        """Strictly read ONE spec by id: returns the parsed Spec, None if no file
        exists for the id, and RAISES SpecLocked (encrypted, no key) or
        SpecParseError (malformed) rather than swallowing them — for callers like
        `mship spec validate` that must report locked/invalid, not silently skip
        (the resilient `list`/`find_by_id` skip both)."""
        from mship.core.spec_storage import spec_id_from_filename
        for path in self._storage.iter_physical():
            if spec_id_from_filename(path) == spec_id:
                return parse_spec(self._storage.decode_file(path))
        return None

    def mutate_inbox(
        self,
        spec_id: str,
        action: InboxAction,
        mutation_id: str,
        now: datetime,
    ) -> Spec:
        """Apply one idempotent inbox mutation without changing the spec lifecycle."""
        with _locked(self._lock_path(spec_id)):
            spec = self.read_strict(spec_id)
            if spec is None:
                raise KeyError(spec_id)
            if apply_inbox_action(spec.inbox, action, mutation_id, now):
                self.save(spec)
            return spec
