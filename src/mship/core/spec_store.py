from __future__ import annotations

import tempfile
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
    """Filesystem registry for markdown-canonical specs under `specs/`."""

    def __init__(self, specs_dir: Path) -> None:
        self._dir = Path(specs_dir)

    def path_for(self, spec: Spec) -> Path:
        """Path for a spec's file: `<specs_dir>/<created_at date>-<id>.md`.

        Saving again with the same id + creation date overwrites the file
        (this is the intended update mechanism).
        """
        if not spec.id or "/" in spec.id or "\\" in spec.id or spec.id in (".", "..") or spec.id.startswith("."):
            raise ValueError(f"unsafe spec id for filename: {spec.id!r}")
        return self._dir / f"{spec.created_at:%Y-%m-%d}-{spec.id}.md"

    def save(self, spec: Spec) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(spec)
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".md.tmp")
        try:
            with open(fd, "w") as f:
                f.write(serialize_spec(spec))
            Path(tmp).replace(path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def load(self, path: Path) -> Spec:
        return parse_spec(Path(path).read_text())

    def list(self) -> list[Spec]:
        if not self._dir.is_dir():
            return []
        return [self.load(p) for p in sorted(self._dir.glob("*.md"))]

    def find_by_id(self, spec_id: str) -> Spec | None:
        for spec in self.list():
            if spec.id == spec_id:
                return spec
        return None
