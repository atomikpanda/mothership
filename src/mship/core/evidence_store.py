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

from mship.util.git import GitRunner

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


# The store is machine-local and lives under the workspace's gitignored state
# directory, NOT in any git repo's tracked tree. That is what makes it behave
# identically in a multi-repo workspace, a monorepo and a single repo: where the
# workspace root happens to BE a product repo, evidence bytes still never enter
# that repo's history. Getting bytes to GitHub for a PR embed is a separate,
# explicit step at finish (core/evidence_url.py), not a property of where the
# file sits.
STATE_DIRNAME = ".mothership"
EVIDENCE_DIRNAME = "evidence"
ENC_SUFFIX = ".enc"

# Extensions we are willing to store, mapped to the content-type we SERVE them
# as. Anything else is refused rather than guessed at.
#
# The non-image entries are the layout dumps `mship capture` produces
# (core/capture.py::KIND_FILENAMES: layout.xml/.json/.html), and they are
# deliberately served as text/plain rather than their true type: their content
# comes from the app under test, so serving it as text/html would let a rendered
# UI string execute script on the API's own origin — a stored-XSS shape. Only
# the image types, which no browser executes, keep a rendering content-type.
_TEXT = "text/plain; charset=utf-8"
CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".xml": _TEXT,
    ".json": _TEXT,
    ".html": _TEXT,
}
IMAGE_EXTS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})

# What a screenshot or layout dump plausibly weighs: a phone PNG at 3x is a few
# MB and an XML dump far less, so 8 MiB accepts every real artifact with room to
# spare while keeping a single blob small enough for a phone to fetch over the
# relay. This is a ceiling on the ARTIFACT, checked both when storing (so the
# operator hears about it at capture time) and when serving (the check that
# actually bounds a response, since a file can reach the store by other means).
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
# On-disk ceiling for an encrypted artifact, whose file is Fernet ciphertext:
# base64 over a 57-byte envelope plus AES padding, so under 1.4x the plaintext.
# 2x is the round number that keeps a just-under-cap artifact servable while
# still bounding what the blob route decrypts into memory.
_CIPHERTEXT_SLACK = 2

_HASH_CHARS = 12


def stored_size_cap(ref: str) -> int:
    """The largest on-disk size this ref may have. Encrypted refs get the
    ciphertext allowance; everything else is the artifact cap itself."""
    if ref.endswith(ENC_SUFFIX):
        return MAX_EVIDENCE_BYTES * _CIPHERTEXT_SLACK
    return MAX_EVIDENCE_BYTES


class EvidenceStoreError(Exception):
    """An artifact could not be stored (unsupported extension, too large,
    unreadable)."""


def evidence_root(workspace_root: Path) -> Path:
    """The store. Every spec's evidence directory is a direct child of this."""
    return Path(workspace_root) / STATE_DIRNAME / EVIDENCE_DIRNAME


def evidence_dir(workspace_root: Path, spec_id: str) -> Path:
    """The one directory a spec's artifact evidence may live in."""
    return evidence_root(workspace_root) / spec_id


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

    `mode` is honoured in full: `encrypted` writes ciphertext under an
    `.enc`-suffixed ref and never plaintext; the other modes plaintext-copy. The
    store is gitignored in every mode — the modes differ only in what LEAVES the
    machine, which is decided at publication (core/evidence_url.py), not here.
    """
    src = Path(src)
    ext = src.suffix.lower()
    if ext not in CONTENT_TYPES:
        raise EvidenceStoreError(
            f"unsupported evidence extension {ext!r}; expected one of "
            f"{', '.join(sorted(CONTENT_TYPES))}"
        )
    size = src.stat().st_size
    if size > MAX_EVIDENCE_BYTES:
        raise EvidenceStoreError(
            f"evidence artifact is {size} bytes, over the "
            f"{MAX_EVIDENCE_BYTES}-byte limit; a screenshot or layout dump this "
            f"large is a capture bug, and the blob route would refuse to serve it"
        )
    ref = f"{_digest(src)}{ext}"
    dest_dir = evidence_dir(workspace_root, spec_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if mode == "encrypted":
        from mship.core import spec_key

        ref = ref + ENC_SUFFIX
        key = spec_key.load_or_generate_key(Path(workspace_root), git=GitRunner())
        (dest_dir / ref).write_bytes(spec_key.encrypt_bytes(key, src.read_bytes()))
        return ref

    shutil.copyfile(src, dest_dir / ref)
    return ref


# A ref is exactly a content hash plus a known extension. `fullmatch` and not a
# `$`-anchored `match`, because `$` would also accept a trailing newline.
# Nothing that fails this ever reaches the filesystem.
_REF_RE = re.compile(r"[0-9a-f]{%d}\.[a-z0-9]{2,5}(\.enc)?" % _HASH_CHARS)


def is_stored_ref(ref: str) -> bool:
    """True when `ref` has the shape this store produces (a content hash plus a
    known extension), as opposed to a hand-written path from before
    `capture --evidence` existed.

    The public answer to "did we store this?", so callers never need the regex
    itself: ref shape stays one module's business.
    """
    return isinstance(ref, str) and _REF_RE.fullmatch(ref) is not None


class BadEvidenceRef(Exception):
    """A ref was malformed, escaped its spec's evidence directory, or is absent."""


def resolve_ref(workspace_root: Path, spec_id: str, ref: str) -> Path:
    """The on-disk path for a stored ref. Raises BadEvidenceRef for anything
    malformed, absent, or resolving outside the spec's evidence directory.

    Both arguments after `workspace_root` arrive from an HTTP path in serve's
    blob route, so both are validated here: `ref` must be a bare hash filename,
    and `spec_id` must name a direct child of the workspace's evidence store.
    """
    if not is_stored_ref(ref):
        raise BadEvidenceRef(f"malformed evidence ref {ref!r}")
    # A full-length hash with an extension we do not serve (`deadbeefcafe.exe`)
    # is refused here, not by the regex. An encrypted ref carries a trailing
    # `.enc`, so the extension we care about is the one beneath that.
    logical = ref[: -len(ENC_SUFFIX)] if ref.endswith(ENC_SUFFIX) else ref
    if Path(logical).suffix.lower() not in CONTENT_TYPES:
        raise BadEvidenceRef(f"unsupported evidence extension in {ref!r}")

    store_root = Path(os.path.realpath(evidence_root(workspace_root)))
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
