import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core import spec_key
from mship.core.spec import Spec
from mship.core.spec_storage import SpecLocked, SpecStorage, spec_id_from_filename
from mship.core.spec_store import SPECS_DIRNAME, SpecStore, serialize_spec


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
    # list()/find_by_id — the readable plaintext siblings still come back.
    from datetime import datetime, timezone
    from mship.core.spec import Spec
    enc = _store(tmp_path, "encrypted")
    enc.save(_spec())                                   # writes a .md.enc + a key
    (tmp_path / ".mothership" / "spec-key").unlink()    # now that .enc is LOCKED
    plain = _store(tmp_path, "committed")
    now = datetime(2026, 7, 22, 10, 0, 0, tzinfo=timezone.utc)
    plain.save(Spec(id="readable-one", title="Readable", status="draft",
                    created_at=now, updated_at=now, body="## Problem\n\nok\n"))
    ids = {s.id for s in plain.list()}
    assert "readable-one" in ids                        # readable sibling survives
    assert "secret-thing" not in ids                    # the locked one is skipped, not crashing
    assert plain.find_by_id("readable-one") is not None


def test_read_strict_raises_locked_and_parse_but_resilient_list_skips(tmp_path: Path):
    # Greptile "Malformed Specs Bypass Validation Output": read_strict must RAISE on
    # a locked or malformed spec (so `mship spec validate` can report it), even though
    # the resilient list() silently skips both.
    enc = _store(tmp_path, "encrypted")
    enc.save(_spec())
    (tmp_path / ".mothership" / "spec-key").unlink()     # secret-thing.enc now LOCKED
    # a malformed plaintext spec (no parseable frontmatter)
    (tmp_path / SPECS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (tmp_path / SPECS_DIRNAME / "2026-07-22-broken.md").write_text("not a valid spec at all")
    store = _store(tmp_path, "committed")
    from mship.core.spec_store import SpecParseError
    with pytest.raises(SpecLocked):
        store.read_strict("secret-thing")
    with pytest.raises(SpecParseError):
        store.read_strict("broken")
    assert store.read_strict("nope") is None            # genuinely-missing id
    # list() stays resilient: neither the locked nor the malformed file aborts it
    assert store.list() == []
