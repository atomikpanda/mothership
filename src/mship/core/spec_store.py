from __future__ import annotations

import yaml
from pathlib import Path
from pydantic import ValidationError

from mship.core.spec import Spec

SPECS_DIRNAME = "specs"  # canonical name of the workspace-level specs directory


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

    def path_for(self, spec: Spec) -> Path:
        """Logical `.md` stem for a spec: `<specs_dir>/<created_at date>-<id>.md`.

        The physical filename (e.g. `.md.enc` under encrypted mode) is resolved by
        the storage layer at write time. Saving again with the same id + creation
        date overwrites the file (this is the intended update mechanism).
        """
        if not spec.id or "/" in spec.id or "\\" in spec.id or spec.id in (".", "..") or spec.id.startswith("."):
            raise ValueError(f"unsafe spec id for filename: {spec.id!r}")
        return self._dir / f"{spec.created_at:%Y-%m-%d}-{spec.id}.md"

    def save(self, spec: Spec) -> Path:
        return self._storage.write(self.path_for(spec), serialize_spec(spec))

    def load(self, path: Path) -> Spec:
        return parse_spec(self._storage.decode_file(Path(path)))

    def list(self) -> list[Spec]:
        # Resilient: skip a spec whose file is locked (encrypted, no key) or
        # unreadable/unparseable, so one bad file never blocks the readable siblings
        # (or every routed CLI op via find_by_id). read_all yields the parsed spec
        # for decodable files and (None, id, path) for locked ones — take the specs.
        return [spec for spec, _locked_id, _path in self._storage.read_all() if spec is not None]

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
