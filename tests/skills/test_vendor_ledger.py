"""The vendor ledger keeps re-vendors honest: every vendored file that differs
from upstream 6.3.0 must be named in VENDOR.md, and banned patterns must never
reappear (spec re-vendor-superpowers-620-with-mship ac3/ac6)."""
import hashlib
import json
from pathlib import Path

SKILLS = Path("src/mship/skills")
MANIFEST = SKILLS / ".upstream-manifest.json"
VENDOR_MD = SKILLS / "VENDOR.md"
ORIGINALS = {"using-mothership", "working-with-mothership",
             "overnight-cloud-worker-routines", "receiving-messages"}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_vendor_ledger_names_every_modified_file():
    manifest = json.loads(MANIFEST.read_text())          # {rel_path: upstream_sha}
    ledger = VENDOR_MD.read_text()
    unledgered = []
    for rel, upstream_sha in manifest.items():
        local = SKILLS / rel
        if not local.exists():
            # deliberately-dropped upstream file: must still be ledgered
            if rel not in ledger:
                unledgered.append(f"{rel} (deleted locally, not in VENDOR.md)")
            continue
        if _sha(local) != upstream_sha and rel not in ledger:
            unledgered.append(rel)
    assert not unledgered, f"files diverge from upstream 6.3.0 without a VENDOR.md entry: {unledgered}"


def test_local_files_not_in_manifest_are_ledgered_or_original():
    manifest = json.loads(MANIFEST.read_text())
    ledger = VENDOR_MD.read_text()
    strays = []
    for p in SKILLS.rglob("*"):
        if not p.is_file() or p.name in ("VENDOR.md", ".upstream-manifest.json"):
            continue
        rel = str(p.relative_to(SKILLS))
        if rel.split("/")[0] in ORIGINALS:
            continue
        if rel not in manifest and rel not in ledger:
            strays.append(rel)
    assert not strays, f"local-only vendored files missing from VENDOR.md: {strays}"


def test_banned_patterns_never_reappear():
    banned = ("superpowers:", ".superpowers/", "Ultrathink")
    hits = []
    for p in SKILLS.rglob("*.md"):
        text = p.read_text()
        for b in banned:
            if b in text:
                hits.append(f"{p}: {b}")
    assert not hits, f"banned patterns in vendored tree: {hits}"


def test_no_references_to_deleted_upstream_files():
    gone = ("testing-anti-patterns.md", "spec-reviewer-prompt.md", "code-quality-reviewer-prompt.md")
    hits = []
    for base in (Path("src/mship/skills"), Path("docs")):
        for p in base.rglob("*.md"):
            if p.name == "VENDOR.md":
                continue                                  # the ledger may name them
            text = p.read_text()
            for g in gone:
                if g in text:
                    hits.append(f"{p}: {g}")
    assert not hits, f"stale references to files deleted in 6.2.0: {hits}"
