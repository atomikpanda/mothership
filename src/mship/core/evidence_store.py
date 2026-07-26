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
import os
import re
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


# A ref is exactly a content hash plus a known extension. `fullmatch` and not a
# `$`-anchored `match`, because `$` would also accept a trailing newline.
# Nothing that fails this ever reaches the filesystem.
_REF_RE = re.compile(r"[0-9a-f]{%d}\.[a-z0-9]{2,5}" % _HASH_CHARS)


class BadEvidenceRef(Exception):
    """A ref was malformed, escaped its spec's evidence directory, or is absent."""


def resolve_ref(workspace_root: Path, spec_id: str, ref: str) -> Path:
    """The on-disk path for a stored ref. Raises BadEvidenceRef for anything
    malformed, absent, or resolving outside the spec's evidence directory.

    Both arguments after `workspace_root` arrive from an HTTP path in serve's
    blob route, so both are validated here: `ref` must be a bare hash filename,
    and `spec_id` must name a direct child of the workspace's evidence store.
    """
    if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
        raise BadEvidenceRef(f"malformed evidence ref {ref!r}")
    # The regex admits one dot only, so the suffix is unambiguous. A full-length
    # hash with an extension we do not serve (`deadbeefcafe.exe`) is refused
    # here, not by the regex.
    if Path(ref).suffix not in CONTENT_TYPES:
        raise BadEvidenceRef(f"unsupported evidence extension in {ref!r}")

    store_root = Path(os.path.realpath(
        Path(workspace_root) / SPECS_DIRNAME / EVIDENCE_DIRNAME
    ))
    root = Path(os.path.realpath(evidence_dir(workspace_root, spec_id)))
    # realpath normalises `..` away, so a spec id like `../other-spec` lands
    # somewhere whose parent is not the store — one spec can never read another's.
    if root.parent != store_root:
        raise BadEvidenceRef(f"malformed spec id {spec_id!r}")

    candidate = root / ref
    # `root` is fully resolved and `ref` is a bare filename, so `candidate` is a
    # direct child of this spec's store; the only way it could still name
    # something else is a symlink at the final component, which is exactly what
    # realpath differing from the path we built detects. A link pointing back
    # INSIDE the store is refused too: a ref attests the content hash of a
    # regular file, so a link there is an integrity violation, and honouring one
    # would make containment depend on where it points at read time.
    if Path(os.path.realpath(candidate)) != candidate:
        raise BadEvidenceRef(f"evidence ref {ref!r} is a link, not stored content")
    if not candidate.is_file():
        raise BadEvidenceRef(f"no evidence at {ref!r}")
    return candidate
