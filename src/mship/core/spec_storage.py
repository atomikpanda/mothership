"""Transparent per-workspace spec storage policy (spec-storage-visibility-policy).

`SpecStorage` decides each spec's on-disk filename + encoding from the workspace
`spec_storage` mode. WRITES honour the mode:
  - committed -> plaintext `specs/<date>-<id>.md`
  - local     -> plaintext `specs/<date>-<id>.md` + ensure `specs/*.md` gitignored
  - encrypted -> Fernet ciphertext `specs/<date>-<id>.md.enc`
READS are suffix-driven, NOT mode-driven: a `.md` is always plaintext, a `.md.enc`
is always decrypted with the key (or reported LOCKED without it). That keeps
half-migrated stores and keyless serve readers correct regardless of the mode.

The mode is a single source of truth: the workspace `spec_storage` config. Every
`SpecStore` built WITHOUT an explicit storage resolves its mode from that config
(`storage_from_workspace`), so every construction site — the spec CLI verbs, serve,
the lifecycle persisters, `cli/worktree.py` — is mode-correct by construction and a
writer under an encrypted workspace can never accidentally emit plaintext.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Iterator, Literal

from mship.core import spec_key
from mship.util.git import GitRunner

SpecMode = Literal["committed", "local", "encrypted"]

# Physical encrypted file is `<date>-<id>.md.enc` (the logical stem keeps `.md`).
ENC_SUFFIX = ".enc"

_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(?P<id>.+)$")


class SpecLocked(Exception):
    """A `.md.enc` spec could not be decoded because no key is present."""

    def __init__(self, spec_id: str) -> None:
        super().__init__(f"spec {spec_id!r} is encrypted and no key is available")
        self.spec_id = spec_id


def spec_id_from_filename(path: Path) -> str:
    """`2026-07-22-foo-bar.md[.enc]` -> `foo-bar`. Best-effort: strips the suffix
    and the leading `YYYY-MM-DD-`, so a locked spec's id is still knowable."""
    name = path.name
    if name.endswith(ENC_SUFFIX):
        name = name[: -len(ENC_SUFFIX)]
    if name.endswith(".md"):
        name = name[: -len(".md")]
    m = _ID_RE.match(name)
    return m.group("id") if m else name


class SpecStorage:
    def __init__(
        self,
        specs_dir: Path,
        mode: SpecMode = "committed",
        *,
        workspace_root: Path | None = None,
        git: GitRunner | None = None,
    ) -> None:
        self.specs_dir = Path(specs_dir)
        self.mode: SpecMode = mode
        # specs_dir is `<workspace_root>/specs`; derive the root when not given so
        # read-only callers (spec_discovery/spec_selection) need no extra plumbing.
        self.workspace_root = Path(workspace_root) if workspace_root else self.specs_dir.parent
        self._git = git or GitRunner()

    # --- path resolution -------------------------------------------------
    def physical_path(self, stem: Path) -> Path:
        """Map a logical `.md` stem (from SpecStore.path_for) to the on-disk file
        for the current WRITE mode."""
        stem = Path(stem)
        if self.mode == "encrypted":
            return stem.with_name(stem.name + ENC_SUFFIX)
        return stem

    def iter_physical(self) -> list[Path]:
        """Every on-disk spec file, both plaintext and ciphertext, sorted."""
        if not self.specs_dir.is_dir():
            return []
        return sorted(
            [*self.specs_dir.glob("*.md"), *self.specs_dir.glob("*.md" + ENC_SUFFIX)]
        )

    # --- write -----------------------------------------------------------
    def write(self, stem: Path, text: str) -> Path:
        self.specs_dir.mkdir(parents=True, exist_ok=True)
        physical = self.physical_path(stem)
        if self.mode == "encrypted":
            key = spec_key.load_or_generate_key(self.workspace_root, git=self._git)
            self._atomic_write_bytes(physical, spec_key.encrypt(key, text))
        else:
            self._atomic_write_text(physical, text)
            if self.mode == "local":
                self._git.add_to_gitignore(self.workspace_root, f"{self.specs_dir.name}/*.md")
        return physical

    # --- read ------------------------------------------------------------
    def decode_file(self, path: Path) -> str:
        """Plaintext of an on-disk spec file. Raises SpecLocked for a `.md.enc`
        with no key — never returns ciphertext or plaintext-fallback."""
        path = Path(path)
        if path.name.endswith(".md" + ENC_SUFFIX):
            key = spec_key.load_key(self.workspace_root)
            if key is None:
                raise SpecLocked(spec_id_from_filename(path))
            return spec_key.decrypt(key, path.read_bytes())
        return path.read_text()

    def read_all(self) -> Iterator[tuple[object | None, str | None, Path]]:
        """Yield (spec_or_None, locked_id_or_None, path) for every spec file.
        Locked (undecryptable) files yield (None, <id>, path); unparseable files
        are skipped. Used by LOCKED-aware readers (serve)."""
        from mship.core.spec_store import SpecParseError, parse_spec

        for path in self.iter_physical():
            try:
                text = self.decode_file(path)
            except SpecLocked as locked:
                yield (None, locked.spec_id, path)
                continue
            try:
                yield (parse_spec(text), None, path)
            except SpecParseError:
                continue

    # --- atomic write helpers (mirror SpecStore.save) --------------------
    def _atomic_write_text(self, path: Path, text: str) -> None:
        self._atomic_write_bytes(path, text.encode("utf-8"))

    def _atomic_write_bytes(self, path: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.specs_dir, suffix=".tmp")
        try:
            with open(fd, "wb") as f:
                f.write(data)
            Path(tmp).replace(path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise


def resolve_mode(workspace_root: Path) -> SpecMode:
    """Best-effort read of `spec_storage` from the workspace's mothership.yaml.

    Single source of truth for the mode when no explicit config object is on hand.
    No mothership.yaml (tests, bare dirs) -> committed (today's behaviour); an
    invalid `spec_storage` value fails LOUD via the config model (never silently
    downgrades to plaintext)."""
    cfg_path = Path(workspace_root) / "mothership.yaml"
    if not cfg_path.is_file():
        return "committed"
    from mship.core.config import ConfigLoader

    config = ConfigLoader.load(cfg_path, require_paths=False)
    return getattr(config, "spec_storage", "committed")


def storage_from_workspace(specs_dir: Path, *, git: GitRunner | None = None) -> "SpecStorage":
    """Build a `SpecStorage` whose mode is resolved from the workspace config at
    `specs_dir.parent`. The DEFAULT construction path for `SpecStore` (no explicit
    storage), so every construction site is mode-correct by construction."""
    specs_dir = Path(specs_dir)
    workspace_root = specs_dir.parent
    return SpecStorage(
        specs_dir, mode=resolve_mode(workspace_root), workspace_root=workspace_root, git=git
    )


def spec_store_from_config(workspace_root: Path, config) -> "SpecStore":
    """Build a mode-aware SpecStore from an already-loaded WorkspaceConfig. Single
    source of the mode->store mapping for callers that already hold the config
    (spec CLI verbs, serve); avoids re-reading mothership.yaml."""
    from mship.core.spec_store import SPECS_DIRNAME, SpecStore

    specs_dir = Path(workspace_root) / SPECS_DIRNAME
    mode = getattr(config, "spec_storage", "committed")
    storage = SpecStorage(specs_dir, mode=mode, workspace_root=Path(workspace_root))
    return SpecStore(specs_dir, storage=storage)
