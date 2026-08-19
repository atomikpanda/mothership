"""The ssh-keygen signature boundary: exact argv, typed failures, and a real
round-trip when ssh-keygen is present (the fake can only prove the argv shape,
not that ssh-keygen accepts it)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mship.core.relay.ssh_sig import (
    SignatureError,
    build_allowed_signers,
    sign_blob,
    verify_blob,
)

NS = "mship-host-registration"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE01 host-a"
PUB2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE02 host-b"


class Recorder:
    """Fake ssh-keygen: records (argv, stdin), returns scripted results.

    Also snapshots the CONTENT of any `-f`/`-s` file while the call is in
    flight — they are temp files the caller deletes on return, so asserting
    after the fact could only see their names."""

    def __init__(self, result=None, raises=None):
        self.calls: list[tuple[list[str], bytes]] = []
        self.files: dict[str, str] = {}
        self._result = result
        self._raises = raises

    def __call__(self, argv, input_bytes):
        self.calls.append((list(argv), input_bytes))
        for flag in ("-f", "-s"):
            if flag in argv:
                path = Path(argv[argv.index(flag) + 1])
                if path.is_file():
                    self.files[flag] = path.read_text()
        if self._raises is not None:
            raise self._raises
        if self._result is not None:
            return self._result
        return subprocess.CompletedProcess(argv, 0, stdout=b"SIG\n", stderr=b"")


def _argv(rec):
    return rec.calls[0][0]


# --- sign ------------------------------------------------------------------


def test_sign_builds_the_ssh_keygen_sign_argv_and_feeds_the_blob_on_stdin(tmp_path):
    rec = Recorder()
    sig = sign_blob(b"payload-bytes", key_path=tmp_path / "k", namespace=NS, runner=rec)
    argv, stdin = rec.calls[0]
    assert argv[:3] == ["ssh-keygen", "-Y", "sign"]
    # Ordering: every flag carries its own value, and stdin ("-") is last so
    # the blob is never mistaken for a flag argument.
    assert argv[argv.index("-f") + 1] == str(tmp_path / "k")
    assert argv[argv.index("-n") + 1] == NS
    assert argv[-1] == "-"
    assert stdin == b"payload-bytes"
    assert sig == "SIG"


def test_sign_raises_typed_error_on_nonzero_exit(tmp_path):
    rec = Recorder(subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"no such key"))
    with pytest.raises(SignatureError) as e:
        sign_blob(b"x", key_path=tmp_path / "k", namespace=NS, runner=rec)
    assert "no such key" in str(e.value)


def test_sign_wraps_a_raising_runner_never_leaking_calledprocesserror(tmp_path):
    rec = Recorder(raises=subprocess.CalledProcessError(1, ["ssh-keygen"]))
    with pytest.raises(SignatureError):
        sign_blob(b"x", key_path=tmp_path / "k", namespace=NS, runner=rec)


def test_sign_wraps_a_missing_ssh_keygen(tmp_path):
    rec = Recorder(raises=FileNotFoundError("ssh-keygen"))
    with pytest.raises(SignatureError):
        sign_blob(b"x", key_path=tmp_path / "k", namespace=NS, runner=rec)


# --- verify ----------------------------------------------------------------


def test_verify_builds_the_ssh_keygen_verify_argv_over_temp_files():
    rec = Recorder()
    assert verify_blob(
        b"payload-bytes",
        signature="SIG",
        identity="SHA256:abc",
        allowed_signers=f"SHA256:abc {PUB}\n",
        namespace=NS,
        runner=rec,
    ) is True
    argv, stdin = rec.calls[0]
    assert argv[:3] == ["ssh-keygen", "-Y", "verify"]
    assert argv[argv.index("-n") + 1] == NS
    assert argv[argv.index("-I") + 1] == "SHA256:abc"
    # The two files exist only for the duration of the call — assert what
    # ssh-keygen actually reads out of them.
    assert rec.files["-f"] == f"SHA256:abc {PUB}\n"
    assert rec.files["-s"] == "SIG\n"
    assert stdin == b"payload-bytes"
    # …and nothing is left behind afterwards.
    assert not Path(argv[argv.index("-f") + 1]).exists()


def test_verify_returns_false_on_a_bad_signature_without_raising():
    rec = Recorder(subprocess.CompletedProcess([], 255, stdout=b"", stderr=b"incorrect signature"))
    assert verify_blob(
        b"x", signature="SIG", identity="SHA256:abc",
        allowed_signers=f"SHA256:abc {PUB}\n", namespace=NS, runner=rec,
    ) is False


def test_verify_wraps_a_raising_runner_in_signature_error():
    rec = Recorder(raises=subprocess.CalledProcessError(1, ["ssh-keygen"]))
    with pytest.raises(SignatureError):
        verify_blob(
            b"x", signature="SIG", identity="SHA256:abc",
            allowed_signers=f"SHA256:abc {PUB}\n", namespace=NS, runner=rec,
        )


def test_verify_never_shells_out_for_an_empty_allowlist():
    """No approved key can possibly have signed it — refuse before spawning."""
    rec = Recorder()
    assert verify_blob(
        b"x", signature="SIG", identity="SHA256:abc",
        allowed_signers="", namespace=NS, runner=rec,
    ) is False
    assert rec.calls == []


# --- allowed_signers -------------------------------------------------------


def test_build_allowed_signers_emits_approved_keys_regardless_of_filename(tmp_path):
    (tmp_path / "a.pub").write_text(PUB + "\n")
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "key-without-suffix").write_text(PUB2 + "\n")
    from mship.core.relay.enroll import fingerprint

    lines = build_allowed_signers(tmp_path).strip().splitlines()
    assert len(lines) == 2
    for line, pub in zip(lines, (PUB, PUB2)):
        principal, ktype, body = line.split()[:3]
        assert principal == fingerprint(pub)
        assert (ktype, body) == tuple(pub.split()[:2])


def test_build_allowed_signers_skips_what_validate_pubkey_rejects(tmp_path):
    (tmp_path / "good.pub").write_text(PUB + "\n")
    (tmp_path / "junk.pub").write_text("not-a-key\n")
    (tmp_path / "injected.pub").write_text(PUB + "\nssh-ed25519 AAAAsmuggled evil\n")
    out = build_allowed_signers(tmp_path)
    assert out.strip().count("\n") == 0
    assert "evil" not in out and "not-a-key" not in out


def test_build_allowed_signers_on_a_missing_dir_is_empty(tmp_path):
    assert build_allowed_signers(tmp_path / "nope") == ""


# --- real round trip -------------------------------------------------------


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not installed")
def test_real_round_trip_signs_and_verifies_against_the_pubkeys_allowlist(tmp_path):
    key = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q"], check=True
    )
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()
    (pubkeys / "host.pub").write_text((tmp_path / "id_ed25519.pub").read_text())
    from mship.core.relay.enroll import fingerprint

    fp = fingerprint((tmp_path / "id_ed25519.pub").read_text())
    signers = build_allowed_signers(pubkeys)
    blob = "hôst-registratiön".encode("utf-8")

    sig = sign_blob(blob, key_path=key, namespace=NS)
    assert verify_blob(blob, signature=sig, identity=fp,
                       allowed_signers=signers, namespace=NS) is True
    # Tampered payload, wrong namespace and unknown identity all fail closed.
    assert verify_blob(blob + b"!", signature=sig, identity=fp,
                       allowed_signers=signers, namespace=NS) is False
    assert verify_blob(blob, signature=sig, identity=fp,
                       allowed_signers=signers, namespace="other-ns") is False
    assert verify_blob(blob, signature=sig, identity="SHA256:nobody",
                       allowed_signers=signers, namespace=NS) is False
