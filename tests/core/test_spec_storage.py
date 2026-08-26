import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core import spec_key
from mship.core.spec import Spec
from mship.core.spec_storage import SpecLocked, SpecStorage, spec_id_from_filename
from mship.core.spec_store import (
    SPECS_DIRNAME, SpecArtifactConflict, SpecParseError, SpecStore, serialize_spec,
)


def _spec():
    now = datetime(2026, 7, 22, 10, 0, 0, tzinfo=timezone.utc)
    return Spec(
        id="secret-thing", title="Secret thing", status="draft",
        created_at=now, updated_at=now,
        body="## Problem\n\nTHE-SECRET-MARKER design intent\n",
    )


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)


def _store(root: Path, mode: str) -> SpecStore:
    specs_dir = root / SPECS_DIRNAME
    storage = SpecStorage(specs_dir, mode=mode, workspace_root=root)
    return SpecStore(specs_dir, storage=storage)


def test_committed_write_is_byte_identical_to_serialize(tmp_path: Path):
    spec = _spec()
    path = _store(tmp_path, "committed").save(spec)
    assert path.name == "2026-07-22-secret-thing.md"
    assert path.read_text() == serialize_spec(spec)


def test_committed_round_trips(tmp_path: Path):
    store = _store(tmp_path, "committed")
    store.save(_spec())
    assert store.find_by_id("secret-thing").body.startswith("## Problem")


def test_encrypted_write_leaves_ciphertext_on_disk(tmp_path: Path):
    """SECURITY: the plaintext markdown must NOT appear in the committed file, and
    the plaintext `.md` path must never be written under encrypted mode."""
    store = _store(tmp_path, "encrypted")
    path = store.save(_spec())
    assert path.name == "2026-07-22-secret-thing.md.enc"
    blob = path.read_bytes()
    assert b"THE-SECRET-MARKER" not in blob
    assert b"## Problem" not in blob
    # The plaintext committed path was never created.
    assert not (tmp_path / SPECS_DIRNAME / "2026-07-22-secret-thing.md").exists()


def test_encrypted_round_trips_with_key(tmp_path: Path):
    store = _store(tmp_path, "encrypted")
    store.save(_spec())
    loaded = store.find_by_id("secret-thing")
    assert "THE-SECRET-MARKER" in loaded.body


def test_create_if_absent_returns_encrypted_physical_path(tmp_path: Path):
    path = _store(tmp_path, "encrypted").create_if_absent(_spec())

    assert path is not None
    assert path.name == "2026-07-22-secret-thing.md.enc"


def test_no_key_holder_cannot_read_encrypted_spec(tmp_path: Path):
    """SECURITY: after the key is removed, decoding yields SpecLocked, never plaintext."""
    store = _store(tmp_path, "encrypted")
    path = store.save(_spec())
    spec_key.keyfile_path(tmp_path).unlink()
    storage = SpecStorage(tmp_path / SPECS_DIRNAME, mode="encrypted", workspace_root=tmp_path)
    with pytest.raises(SpecLocked) as exc:
        storage.decode_file(path)
    assert exc.value.spec_id == "secret-thing"


def test_encrypted_read_without_key_never_returns_plaintext(tmp_path: Path):
    store = _store(tmp_path, "encrypted")
    path = store.save(_spec())
    spec_key.keyfile_path(tmp_path).unlink()
    # Nothing on disk or reachable exposes the marker.
    assert b"THE-SECRET-MARKER" not in path.read_bytes()


def test_local_write_is_plaintext_but_gitignored_and_untracked(tmp_path: Path):
    """SECURITY: local mode is fully readable locally yet never a committable file."""
    _git_init(tmp_path)
    store = _store(tmp_path, "local")
    path = store.save(_spec())
    assert path.name == "2026-07-22-secret-thing.md"
    assert "THE-SECRET-MARKER" in path.read_text()  # plaintext, usable locally
    # Gitignored:
    check = subprocess.run(
        ["git", "check-ignore", "-q", str(path.relative_to(tmp_path))], cwd=tmp_path
    )
    assert check.returncode == 0
    # And absent from `git status` as a trackable file:
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "secret-thing" not in status


def test_read_is_suffix_driven_across_mixed_store(tmp_path: Path):
    """A committed .md and an encrypted .md.enc coexist (mid-migration); list() surfaces both."""
    _store(tmp_path, "committed").save(_spec())
    other = _spec()
    other.id = "also-secret"
    _store(tmp_path, "encrypted").save(other)
    ids = {s.id for s in _store(tmp_path, "committed").list()}
    assert ids == {"secret-thing", "also-secret"}


def test_spec_id_from_filename():
    assert spec_id_from_filename(Path("2026-07-22-foo-bar.md")) == "foo-bar"
    assert spec_id_from_filename(Path("2026-07-22-foo-bar.md.enc")) == "foo-bar"


# --- SECURITY OVERRIDE: writer-funnel guard (default-construction is mode-aware) ---

def test_direct_specstore_construction_is_config_mode_aware(tmp_path: Path):
    """SECURITY GUARD (writer-funnel): a SpecStore built with NO explicit storage
    under an encrypted-config workspace must still write ciphertext. This is what
    makes EVERY `SpecStore(specs_dir)` construction site (cli/worktree.py, the
    spec_lifecycle / workitem_lifecycle persisters, ...) mode-correct by
    construction — never an accidental plaintext leak."""
    (tmp_path / "mothership.yaml").write_text(
        "workspace: demo\nspec_storage: encrypted\n"
    )
    store = SpecStore(tmp_path / SPECS_DIRNAME)  # no explicit storage
    path = store.save(_spec())
    assert path.name.endswith(".md.enc")
    assert b"THE-SECRET-MARKER" not in path.read_bytes()
    # No plaintext committed representation was written.
    assert list((tmp_path / SPECS_DIRNAME).glob("*.md")) == []


def test_direct_specstore_construction_defaults_committed_without_config(tmp_path: Path):
    """No mothership.yaml (tests, bare dirs) -> committed, preserving today's
    plaintext behaviour."""
    store = SpecStore(tmp_path / SPECS_DIRNAME)  # no config, no explicit storage
    path = store.save(_spec())
    assert path.name == "2026-07-22-secret-thing.md"
    assert "THE-SECRET-MARKER" in path.read_text()


def test_no_module_serializes_specs_outside_storage_layer():
    """SECURITY GUARD: the ONLY on-disk spec writer is SpecStore.save -> SpecStorage.

    `serialize_spec` is the single spec codec; any module that calls it to persist
    a spec would bypass the storage layer and, under encrypted mode, emit
    plaintext — the exact leak this feature prevents. Allowlist:
      - core/spec_store.py: the codec itself + SpecStore.save (goes through storage)
      - core/export.py:     writes a redacted `spec.md` into an EXPORT BUNDLE, never
                            the `specs/` store.
    A new caller trips this test — route it through SpecStore (which delegates to
    the mode-aware SpecStorage) instead of serializing + writing by hand.
    """
    import mship

    src_root = Path(mship.__file__).parent
    allowed = {"core/spec_store.py", "core/export.py"}
    offenders = [
        py.relative_to(src_root).as_posix()
        for py in src_root.rglob("*.py")
        if py.relative_to(src_root).as_posix() not in allowed
        and "serialize_spec(" in py.read_text()
    ]
    assert offenders == [], (
        f"modules serialize specs outside the storage layer: {offenders}"
    )


def test_list_skips_locked_file_and_returns_readable_siblings(tmp_path: Path):
    # Greptile "One Locked File Blocks All Specs": a locked .md.enc must not abort
    # list()/find_by_id — an exact readable sibling remains addressable.
    from datetime import datetime, timezone
    from mship.core.spec import Spec

    plain = _store(tmp_path, "committed")
    now = datetime(2026, 7, 22, 10, 0, 0, tzinfo=timezone.utc)
    plain.save(Spec(id="readable-one", title="Readable", status="draft",
                    created_at=now, updated_at=now, body="## Problem\n\nok\n"))
    enc = _store(tmp_path, "encrypted")
    enc.save(_spec())                                   # writes a .md.enc + a key
    (tmp_path / ".mothership" / "spec-key").unlink()    # now that .enc is LOCKED

    ids = {s.id for s in plain.list()}
    assert "readable-one" in ids                        # readable sibling survives
    assert "secret-thing" not in ids                    # the locked one is skipped, not crashing
    assert plain.find_by_id("readable-one") is not None


def test_read_strict_raises_while_tolerant_list_skips_locked_and_malformed(tmp_path: Path):
    # Strict readers surface unavailable artifacts to lifecycle/API gates. Tolerant
    # lists preserve readable siblings for CLI/display.
    enc = _store(tmp_path, "encrypted")
    enc.save(_spec())
    (tmp_path / ".mothership" / "spec-key").unlink()     # secret-thing.enc now LOCKED
    # a malformed plaintext spec (no parseable frontmatter)
    (tmp_path / SPECS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (tmp_path / SPECS_DIRNAME / "2026-07-22-broken.md").write_text("not a valid spec at all")
    store = _store(tmp_path, "committed")
    with pytest.raises(SpecLocked):
        store.read_strict("secret-thing")
    with pytest.raises(SpecParseError):
        store.read_strict("broken")
    assert store.read_strict("nope") is None            # unrelated locked canonical is ignorable
    assert store.list() == []

def test_local_lifecycle_save_reapplies_gitignore_for_renamed_artifact(tmp_path: Path):
    _git_init(tmp_path)
    store = _store(tmp_path, "local")
    spec = _spec()
    path = store.save(spec)
    renamed = path.with_name("legacy.md")
    path.rename(renamed)
    (tmp_path / ".gitignore").write_text("kept-rule\n")
    spec.status = "needs_review"

    assert store.save(spec) == renamed
    assert (tmp_path / ".gitignore").read_text() == "kept-rule\nspecs/*.md\n"
    assert subprocess.run(
        ["git", "check-ignore", "-q", str(renamed.relative_to(tmp_path))], cwd=tmp_path,
    ).returncode == 0


def test_encrypted_artifact_does_not_fallback_to_plaintext_under_committed_policy(tmp_path: Path):
    encrypted = _store(tmp_path, "encrypted")
    spec = _spec()
    encrypted_path = encrypted.save(spec)
    committed = SpecStore(tmp_path / SPECS_DIRNAME)

    with pytest.raises(SpecArtifactConflict):
        committed.save(spec)

    assert encrypted_path.is_file()
    assert not encrypted_path.with_suffix("").exists()


def test_find_by_id_tolerates_locked_exact_artifact_but_read_strict_does_not(tmp_path: Path):
    store = _store(tmp_path, "encrypted")
    store.save(_spec())
    spec_key.keyfile_path(tmp_path).unlink()

    assert store.find_by_id("secret-thing") is None
    with pytest.raises(SpecLocked):
        store.read_strict("secret-thing")


def test_create_refuses_any_locked_ciphertext_without_creating_a_replacement_key(tmp_path: Path):
    store = _store(tmp_path, "encrypted")
    store.save(_spec())
    spec_key.keyfile_path(tmp_path).unlink()
    new_spec = _spec()
    new_spec.id = "different-id"

    with pytest.raises(SpecLocked):
        store.create_if_absent(new_spec)

    assert spec_key.load_key(tmp_path) is None
    assert not (tmp_path / SPECS_DIRNAME / "2026-07-22-different-id.md.enc").exists()


def test_storage_write_refuses_to_generate_key_when_ciphertext_exists(tmp_path: Path):
    store = _store(tmp_path, "encrypted")
    store.save(_spec())
    spec_key.keyfile_path(tmp_path).unlink()
    storage = SpecStorage(tmp_path / SPECS_DIRNAME, mode="encrypted", workspace_root=tmp_path)

    with pytest.raises(SpecLocked):
        storage.write(tmp_path / SPECS_DIRNAME / "2026-07-22-direct.md", "body")

    assert spec_key.load_key(tmp_path) is None


def test_create_refuses_unknown_locked_alias_without_creating_a_replacement_key(tmp_path: Path):
    store = _store(tmp_path, "encrypted")
    locked_path = store.save(_spec())
    spec_key.keyfile_path(tmp_path).unlink()
    locked_path.rename(locked_path.with_name("renamed.md.enc"))
    new_spec = _spec()
    new_spec.id = "real-id"

    with pytest.raises(SpecArtifactConflict):
        store.create_if_absent(new_spec)

    assert spec_key.load_key(tmp_path) is None
    assert not (tmp_path / SPECS_DIRNAME / "2026-07-22-real-id.md.enc").exists()


def test_exact_readable_artifact_remains_resolvable_beside_unrelated_locked_sibling(tmp_path: Path):
    readable = _spec()
    readable.id = "readable"
    committed = _store(tmp_path, "committed")
    committed.save(readable)
    encrypted = _store(tmp_path, "encrypted")
    encrypted.save(_spec())
    spec_key.keyfile_path(tmp_path).unlink()

    assert committed.read_strict("readable").id == "readable"


def test_invalid_encrypted_token_is_a_controlled_parse_error_and_tolerant_scan_skips_it(tmp_path: Path):
    encrypted = _store(tmp_path, "encrypted")
    broken = encrypted.save(_spec())
    broken.write_bytes(b"not a valid fernet token")
    readable = _spec()
    readable.id = "readable"
    _store(tmp_path, "committed").save(readable)

    with pytest.raises(SpecParseError) as exc:
        encrypted.read_strict("secret-thing")

    assert "not a valid fernet token" not in str(exc.value)
    assert [spec.id for spec, _, _ in encrypted._storage.read_all()] == ["readable"]
    assert [spec.id for spec in encrypted.list()] == ["readable"]


def test_readable_exact_conflicts_with_locked_renamed_artifact(tmp_path: Path):
    committed = _store(tmp_path, "committed")
    readable = _spec()
    readable.id = "readable"
    committed.save(readable)
    encrypted = _store(tmp_path, "encrypted")
    locked = encrypted.save(_spec())
    spec_key.keyfile_path(tmp_path).unlink()
    locked.rename(locked.with_name("legacy.md.enc"))

    with pytest.raises(SpecArtifactConflict):
        committed.read_strict("readable")


def test_locked_exact_conflicts_with_readable_alias_for_same_id(tmp_path: Path):
    encrypted = _store(tmp_path, "encrypted")
    spec = _spec()
    locked = encrypted.save(spec)
    locked.with_name("legacy.md").write_text(serialize_spec(spec))
    spec_key.keyfile_path(tmp_path).unlink()

    with pytest.raises(SpecArtifactConflict):
        encrypted.read_strict(spec.id)


def test_readable_renamed_artifact_ignores_locked_canonical_sibling_for_different_id(tmp_path: Path):
    readable = _spec()
    readable.id = "readable"
    committed = _store(tmp_path, "committed")
    canonical = committed.save(readable)
    canonical.rename(canonical.with_name("legacy-readable.md"))
    encrypted = _store(tmp_path, "encrypted")
    encrypted.save(_spec())
    spec_key.keyfile_path(tmp_path).unlink()

    assert committed.read_strict("readable").id == "readable"


def test_canonical_filename_frontmatter_mismatch_conflicts_for_both_ids(tmp_path: Path):
    store = _store(tmp_path, "committed")
    frontmatter = _spec()
    frontmatter.id = "frontmatter-id"
    path = tmp_path / SPECS_DIRNAME / "2026-07-22-physical-id.md"
    path.parent.mkdir(parents=True)
    path.write_text(serialize_spec(frontmatter))

    with pytest.raises(SpecArtifactConflict):
        store.read_strict("physical-id")
    with pytest.raises(SpecArtifactConflict):
        store.read_strict("frontmatter-id")

def test_plaintext_invalid_utf8_is_parse_error_and_tolerant_reads_skip_it(tmp_path: Path):
    specs_dir = tmp_path / SPECS_DIRNAME
    specs_dir.mkdir()
    (specs_dir / "2026-07-22-broken.md").write_bytes(b"\xff")
    readable = _spec()
    readable.id = "readable"
    store = _store(tmp_path, "committed")
    store.save(readable)

    with pytest.raises(SpecParseError):
        store.read_strict("broken")

    assert [spec.id for spec in store.list()] == ["readable"]
    assert [spec.id for spec, _, _ in store._storage.read_all()] == ["readable"]


def test_malformed_encryption_key_is_parse_error_for_reads_and_writes(tmp_path: Path):
    encrypted = _store(tmp_path, "encrypted")
    encrypted.save(_spec())
    spec_key.keyfile_path(tmp_path).write_bytes(b"malformed-fernet-key")

    with pytest.raises(SpecParseError):
        encrypted.read_strict("secret-thing")
    assert encrypted.list() == []
    assert list(encrypted._storage.read_all()) == []

    another = _spec()
    another.id = "another"
    with pytest.raises(SpecParseError):
        encrypted.save(another)


def test_tolerant_scan_skips_canonical_filename_frontmatter_mismatch(tmp_path: Path):
    mismatched = _spec()
    mismatched.id = "frontmatter-id"
    path = tmp_path / SPECS_DIRNAME / "2026-07-22-physical-id.md"
    path.parent.mkdir()
    path.write_text(serialize_spec(mismatched))

    assert list(_store(tmp_path, "committed")._storage.read_all()) == []


def test_list_fails_closed_on_canonical_mismatch_and_duplicate_ids(tmp_path: Path):
    mismatched = _spec()
    mismatched.id = "frontmatter-id"
    specs_dir = tmp_path / SPECS_DIRNAME
    specs_dir.mkdir()
    (specs_dir / "2026-07-22-physical-id.md").write_text(serialize_spec(mismatched))
    with pytest.raises(SpecArtifactConflict):
        _store(tmp_path, "committed").list()

    (specs_dir / "2026-07-22-physical-id.md").unlink()
    duplicate = _spec()
    duplicate.id = "same-id"
    (specs_dir / "2026-07-22-same-id.md").write_text(serialize_spec(duplicate))
    (specs_dir / "2026-07-23-same-id.md").write_text(serialize_spec(duplicate))
    with pytest.raises(SpecArtifactConflict):
        _store(tmp_path, "committed").list()
