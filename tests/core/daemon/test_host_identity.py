"""Host identity (#471 Task 1): minted, machine-bound, workspace-free.

The three-net clone story lives here in part: net (a) — this file — catches a
RE-IMAGED host (fingerprint changed). A `cp -a` clone copies the fingerprint
verbatim, so net (a) deliberately does NOT fire for it; that is the relay's job
(net (b), Task 4). Both are asserted below so the boundary is explicit.
"""
import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.daemon.identity import (
    ensure_host_identity,
    force_reidentify,
    machine_fingerprint,
    mint_host_id,
    mint_instance_id,
)
from mship.core.daemon.paths import host_identity_path
from mship.core.relay.tls_ask import tls_ask_allowed
from mship.core.relay.tunnel import device_subdomain, host_subdomain

NOW = datetime(2026, 8, 17, 18, 0, tzinfo=timezone.utc)


def _noop_rotate(home):
    pass


def test_mint_on_empty_home(tmp_path: Path):
    ident = ensure_host_identity(tmp_path, fingerprint="fp-A", now=NOW, rotate_key=_noop_rotate)
    assert ident.host_id.startswith("hst-20260817")
    assert ident.fingerprint == "fp-A"
    path = host_identity_path(tmp_path)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert json.loads(path.read_text())["host_id"] == ident.host_id


def test_identity_write_retries_short_writes_and_syncs_file_and_directory(
    tmp_path: Path, monkeypatch
):
    from mship.core.daemon import identity as identity_mod

    real_write = identity_mod.os.write
    write_sizes = []
    fsynced = []

    def short_write(fd, data):
        chunk = bytes(data[:3])
        write_sizes.append(len(chunk))
        return real_write(fd, chunk)

    monkeypatch.setattr(identity_mod.os, "write", short_write)
    monkeypatch.setattr(identity_mod.os, "fsync", lambda fd: fsynced.append(fd))

    ident = ensure_host_identity(
        tmp_path, fingerprint="fp-A", now=NOW, rotate_key=_noop_rotate
    )

    assert len(write_sizes) > 1
    assert (
        json.loads(host_identity_path(tmp_path).read_text())["host_id"]
        == ident.host_id
    )
    assert len(fsynced) == 2


def test_idempotent_and_per_home(tmp_path: Path):
    a1 = ensure_host_identity(tmp_path / "a", fingerprint="fp", rotate_key=_noop_rotate)
    a2 = ensure_host_identity(tmp_path / "a", fingerprint="fp", rotate_key=_noop_rotate)
    b = ensure_host_identity(tmp_path / "b", fingerprint="fp", rotate_key=_noop_rotate)
    assert a1.host_id == a2.host_id
    assert b.host_id != a1.host_id  # AC5 at the identity layer


def test_reimage_reidentifies_once_and_rotates_key(tmp_path: Path):
    rotated = []
    first = ensure_host_identity(tmp_path, fingerprint="A", rotate_key=lambda h: rotated.append(h))
    second = ensure_host_identity(tmp_path, fingerprint="B", rotate_key=lambda h: rotated.append(h))
    assert second.host_id != first.host_id
    assert second.cloned_from == first.host_id
    assert second.reidentified is True
    # The RUNNING machine's fingerprint is what gets recorded — the branch now
    # delegates to force_reidentify, whose fallback would otherwise carry the
    # stale one forward and re-fire the mismatch on every call.
    assert second.fingerprint == "B"
    assert json.loads(host_identity_path(tmp_path).read_text())["fingerprint"] == "B"
    assert rotated == [tmp_path]  # this host must enroll under independent key material
    third = ensure_host_identity(tmp_path, fingerprint="B", rotate_key=lambda h: rotated.append(h))
    assert third.host_id == second.host_id  # stable; no re-identify loop
    assert third.reidentified is False
    assert rotated == [tmp_path]


def test_forced_reidentify_preserves_a_possibly_shared_relay_key_approval(
    tmp_path: Path, monkeypatch
):
    from mship.core.daemon import relay_link
    from mship.core.daemon.registry import DaemonConfig, save_daemon_config

    ensure_host_identity(tmp_path, fingerprint="same", rotate_key=_noop_rotate)
    save_daemon_config(tmp_path, DaemonConfig(relay={"host": "relay.example"}))
    revoked = []
    monkeypatch.setattr(
        relay_link,
        "revoke_relay_key",
        lambda home, relay: revoked.append((home, relay)),
    )

    force_reidentify(tmp_path, rotate_key=_noop_rotate, now=NOW)

    assert revoked == []


def test_real_key_rotation_moves_key_aside(tmp_path: Path):
    from mship.core.relay.keys import relay_key_path

    key = relay_key_path(tmp_path)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("PRIVATE")
    key.with_name(key.name + ".pub").write_text("ssh-ed25519 AAAAC3Nz mship-relay")
    ensure_host_identity(tmp_path, fingerprint="A")
    ensure_host_identity(tmp_path, fingerprint="B")
    assert not key.exists()
    assert any(p.name.startswith("relay_ed25519.pre-reidentify-") for p in key.parent.iterdir())


def test_keep_adopts_without_rotating(tmp_path: Path):
    rotated = []
    first = ensure_host_identity(tmp_path, fingerprint="A", rotate_key=lambda h: rotated.append(h))
    kept = ensure_host_identity(
        tmp_path, fingerprint="B", on_mismatch="keep", rotate_key=lambda h: rotated.append(h)
    )
    assert kept.host_id == first.host_id
    assert kept.adopted_fingerprint == "B"
    assert rotated == []
    # the adoption persisted: B is now the recorded fingerprint
    assert ensure_host_identity(tmp_path, fingerprint="B", rotate_key=_noop_rotate).host_id == first.host_id


def test_absent_fingerprint_is_never_a_mismatch(tmp_path: Path):
    """Containers report no machine-id; that must not raise a clone alarm."""
    first = ensure_host_identity(tmp_path, fingerprint=None, rotate_key=_noop_rotate)
    again = ensure_host_identity(tmp_path, fingerprint=None, rotate_key=_noop_rotate)
    assert again.host_id == first.host_id
    assert again.reidentified is False
    # and a host that starts reporting one later is not treated as a clone
    later = ensure_host_identity(tmp_path, fingerprint="A", rotate_key=_noop_rotate)
    assert later.host_id == first.host_id


def test_fingerprint_identical_clone_does_not_fire_net_a(tmp_path: Path):
    """`cp -a` copies /etc/machine-id verbatim: net (a) deliberately cannot see
    it, which is precisely why the relay's live-claimant arbitration exists."""
    src = tmp_path / "src"
    ensure_host_identity(src, fingerprint="same", rotate_key=_noop_rotate)
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / ".mothership" / "daemon").mkdir(parents=True)
    host_identity_path(clone).write_bytes(host_identity_path(src).read_bytes())
    cloned = ensure_host_identity(clone, fingerprint="same", rotate_key=_noop_rotate)
    assert cloned.reidentified is False
    assert cloned.host_id == ensure_host_identity(src, fingerprint="same", rotate_key=_noop_rotate).host_id


def test_corrupt_file_remints(tmp_path: Path):
    path = host_identity_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{truncated")
    ident = ensure_host_identity(tmp_path, fingerprint="A", rotate_key=_noop_rotate)
    assert ident.host_id.startswith("hst-")


def test_instance_id_is_per_process_and_never_persisted(tmp_path: Path):
    a, b = mint_instance_id(), mint_instance_id()
    assert a != b and len(a) == 16
    ensure_host_identity(tmp_path, fingerprint="A", rotate_key=_noop_rotate)
    raw = host_identity_path(tmp_path).read_text()
    assert a not in raw and "instance" not in raw  # a clone must not be able to copy it


def test_machine_fingerprint_reads_first_available(tmp_path: Path):
    empty, good = tmp_path / "empty", tmp_path / "good"
    empty.write_text("   \n")
    good.write_text("abc123\n")
    assert machine_fingerprint([tmp_path / "absent", empty, good]) == "abc123"
    assert machine_fingerprint([tmp_path / "absent"]) is None


def test_host_subdomain_shape_and_tls(tmp_path: Path):
    secret = b"x" * 32
    host_id = mint_host_id(NOW)
    sub = host_subdomain(host_id, "abc123", secret)
    assert len(sub) <= 63
    assert sub != device_subdomain("some-workspace", "abc123", secret)
    # AC3: the existing cert allowlist already admits this shape — no TLS change
    assert tls_ask_allowed(f"{sub}.relay.example.com", "relay.example.com")


# Every host-level module #471 adds. Extend this list as tasks 3+ land — the
# invariant is the point, not the one file it started on.
WORKSPACE_FREE_MODULES = (
    "identity.py",
    "host_token.py",
    "host_auth.py",
    "capabilities.py",
    "relay_link.py",
    "host_tunnel.py",
)

_FORBIDDEN_IMPORTS = ("mship.core.config", "mship.core.workspace_context")
_FORBIDDEN_ARGS = {"workspace", "repos", "config", "workspace_root"}


@pytest.mark.parametrize("module_name", WORKSPACE_FREE_MODULES)
def test_host_modules_are_workspace_free(module_name):
    """Assumption-header invariant: the host tier must not depend on which
    workspaces this daemon serves."""
    src = (
        Path(__file__).resolve().parents[3]
        / "src" / "mship" / "core" / "daemon" / module_name
    )
    tree = ast.parse(src.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(m in imported for m in _FORBIDDEN_IMPORTS), module_name
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = {a.arg for a in node.args.args + node.args.kwonlyargs}
            assert not (args & _FORBIDDEN_ARGS), f"{module_name}:{node.name}"
