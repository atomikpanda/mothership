from pathlib import Path

import pytest

from mship.core import spec_key
from mship.core.spec_key import SpecKeyMissing


def test_load_key_returns_none_when_absent(tmp_path: Path):
    assert spec_key.load_key(tmp_path) is None


def test_require_key_raises_when_absent(tmp_path: Path):
    with pytest.raises(SpecKeyMissing):
        spec_key.require_key(tmp_path)


def test_generate_creates_gitignored_keyfile_with_loud_notice(tmp_path: Path, capsys):
    key = spec_key.load_or_generate_key(tmp_path)
    keyfile = spec_key.keyfile_path(tmp_path)
    assert keyfile.is_file()
    assert keyfile.read_bytes() == key
    # 0600 perms so a stray key isn't world-readable.
    assert (keyfile.stat().st_mode & 0o077) == 0
    # Loud, one-time backup notice on first generation (stderr).
    err = capsys.readouterr().err
    assert "BACK THIS FILE UP" in err
    assert "unrecoverable" in err.lower()
    # Ensured gitignored: an entry exists (this tmp dir is not a git repo, so the
    # module falls back to appending to .gitignore).
    assert ".mothership/spec-key" in (tmp_path / ".gitignore").read_text()


def test_generate_is_idempotent_and_quiet_second_time(tmp_path: Path, capsys):
    first = spec_key.load_or_generate_key(tmp_path)
    capsys.readouterr()  # drain the first-generation notice
    second = spec_key.load_or_generate_key(tmp_path)
    assert first == second
    assert "BACK THIS FILE UP" not in capsys.readouterr().err


def test_encrypt_output_excludes_plaintext_and_round_trips(tmp_path: Path):
    key = spec_key.load_or_generate_key(tmp_path)
    plaintext = "## Problem\n\nsecret design intent\n"
    blob = spec_key.encrypt(key, plaintext)
    assert b"secret design intent" not in blob
    assert spec_key.decrypt(key, blob) == plaintext


def test_generated_key_is_0600(tmp_path: Path):
    # Greptile: the key must be 0600 from creation (no world-readable window).
    import os, stat
    spec_key.load_or_generate_key(tmp_path)
    mode = stat.S_IMODE(os.stat(spec_key.keyfile_path(tmp_path)).st_mode)
    assert mode == 0o600


def test_generate_when_key_exists_reuses_it(tmp_path: Path):
    # Greptile: a second first-write must reuse the existing key, never split.
    k1 = spec_key.load_or_generate_key(tmp_path)
    k2 = spec_key.load_or_generate_key(tmp_path)
    assert k1 == k2


def test_generate_lost_race_reuses_winners_key(tmp_path: Path, monkeypatch):
    # Greptile "Concurrent Writers Split The Key": if the keyfile appears between the
    # load-None check and the atomic link (a concurrent winner), the loser reuses the
    # winner's key rather than clobbering it with a second key.
    import os as _os
    winner = spec_key.Fernet.generate_key()
    real_link = _os.link

    def racing_link(src, dst):
        p = spec_key.keyfile_path(tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_bytes(winner)
        return real_link(src, dst)  # raises FileExistsError

    monkeypatch.setattr(_os, "link", racing_link)
    assert spec_key.load_or_generate_key(tmp_path) == winner
