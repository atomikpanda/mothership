"""The two view readers that bypass SpecStore (spec_discovery, spec_selection)
must be suffix-aware: an encrypted `.md.enc` spec is discoverable + decrypted,
and a locked (keyless) encrypted spec is simply skipped — never a raw-ciphertext
parse error (spec-storage-visibility-policy ac2)."""
from datetime import datetime, timezone
from pathlib import Path

from mship.core import spec_key
from mship.core.spec import Spec
from mship.core.spec_storage import SpecStorage
from mship.core.spec_store import SPECS_DIRNAME, SpecStore
from mship.core.view.spec_discovery import _find_in_specs_dir
from mship.core.view.spec_selection import scan_canonical_specs


def _write_encrypted(root: Path, spec_id: str, task_slug: str | None = None) -> None:
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    spec = Spec(id=spec_id, title=spec_id, status="draft",
                created_at=now, updated_at=now, task_slug=task_slug,
                body="## Problem\n\nENCRYPTED-BODY\n")
    storage = SpecStorage(root / SPECS_DIRNAME, mode="encrypted", workspace_root=root)
    SpecStore(root / SPECS_DIRNAME, storage=storage).save(spec)


def test_scan_canonical_specs_surfaces_encrypted_spec(tmp_path: Path):
    _write_encrypted(tmp_path, "enc-one")
    pairs = scan_canonical_specs(tmp_path / SPECS_DIRNAME)
    ids = [s.id for s, _ in pairs]
    assert ids == ["enc-one"]
    assert "ENCRYPTED-BODY" in pairs[0][0].body


def test_scan_canonical_specs_skips_locked_spec(tmp_path: Path):
    _write_encrypted(tmp_path, "enc-one")
    spec_key.keyfile_path(tmp_path).unlink()
    # No key: the encrypted spec is skipped, not surfaced as ciphertext/garbage.
    assert scan_canonical_specs(tmp_path / SPECS_DIRNAME) == []


def test_find_in_specs_dir_finds_encrypted_by_id(tmp_path: Path):
    _write_encrypted(tmp_path, "enc-two")
    found = _find_in_specs_dir(tmp_path, spec_id="enc-two")
    assert found is not None and found.name.endswith(".md.enc")


def test_find_in_specs_dir_finds_encrypted_by_task_slug(tmp_path: Path):
    _write_encrypted(tmp_path, "enc-three", task_slug="my-task")
    found = _find_in_specs_dir(tmp_path, task_slug="my-task")
    assert found is not None and found.name.endswith(".md.enc")
