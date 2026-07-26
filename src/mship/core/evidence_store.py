"""Where acceptance-criterion artifact evidence lives, and how a ref resolves
back to it.

Single owner of the path math: both `mship capture --evidence` and serve's blob
route go through this module, so nothing else computes an evidence path.

The persisted ref is a BARE FILENAME, never a path. Because the resolver joins
exactly one root — the spec's own evidence directory — a ref has no way to
express a location outside it. Validation still rejects malformed names, but the
primary defence is that the data model cannot say "elsewhere".
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Literal

EvidenceMode = Literal["committed", "local", "encrypted"]

# Ordered least-exposed to most-exposed. `local` never leaves the machine;
# `encrypted` leaves but is unreadable without the key; `committed` leaves in the
# clear. Evidence may never rank above the spec it backs.
_EXPOSURE: dict[str, int] = {"local": 0, "encrypted": 1, "committed": 2}


class EvidenceModeError(Exception):
    """The configured evidence_storage is more exposed than spec_storage."""


def resolve_evidence_mode(config) -> EvidenceMode:
    """The effective evidence mode. `evidence_storage` unset inherits
    `spec_storage`; set, it must not be more exposed than the spec's mode."""
    spec_mode: EvidenceMode = getattr(config, "spec_storage", "committed")
    declared = getattr(config, "evidence_storage", None)
    if declared is None:
        return spec_mode
    if _EXPOSURE[declared] > _EXPOSURE[spec_mode]:
        raise EvidenceModeError(
            f"evidence_storage={declared!r} is more exposed than "
            f"spec_storage={spec_mode!r}. A screenshot discloses what the spec "
            f"prose was protecting, so evidence may never be less protected "
            f"than its spec. Use one of: "
            f"{', '.join(m for m in _EXPOSURE if _EXPOSURE[m] <= _EXPOSURE[spec_mode])}."
        )
    return declared


SPECS_DIRNAME = "specs"
EVIDENCE_DIRNAME = "evidence"
ENC_SUFFIX = ".enc"

# Extensions we are willing to store and serve. Anything else is refused rather
# than guessed at — a served blob's content-type is derived from this.
CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".xml": "application/xml",
    ".json": "application/json",
    ".html": "text/html",
}
IMAGE_EXTS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})

_HASH_CHARS = 12


class EvidenceStoreError(Exception):
    """An artifact could not be stored (unsupported extension, unreadable)."""


def evidence_dir(workspace_root: Path, spec_id: str) -> Path:
    """The one directory a spec's artifact evidence may live in."""
    return Path(workspace_root) / SPECS_DIRNAME / EVIDENCE_DIRNAME / spec_id


def _digest(src: Path) -> str:
    h = hashlib.sha256()
    with open(src, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:_HASH_CHARS]


def store_artifact(
    workspace_root: Path, spec_id: str, src: Path, *, mode: EvidenceMode
) -> str:
    """Copy `src` into the spec's evidence directory under a content-hashed
    name. Returns the BARE FILENAME to persist as the evidence ref.

    `mode` is accepted here and honoured in full by a later change (gitignore for
    `local`, ciphertext for `encrypted`); this path is the plaintext copy.
    """
    src = Path(src)
    ext = src.suffix.lower()
    if ext not in CONTENT_TYPES:
        raise EvidenceStoreError(
            f"unsupported evidence extension {ext!r}; expected one of "
            f"{', '.join(sorted(CONTENT_TYPES))}"
        )
    ref = f"{_digest(src)}{ext}"
    dest_dir = evidence_dir(workspace_root, spec_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest_dir / ref)
    return ref
