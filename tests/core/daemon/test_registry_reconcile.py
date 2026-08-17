"""Reconciliation + identity (#472 Task 4): id files, moves, copies, missing."""
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mship.core.daemon.discovery import scan_roots
from mship.core.daemon.paths import registry_path
from mship.core.daemon.registry import (
    ID_FILE_RELPATH,
    DaemonConfig,
    RegistryStore,
    reconcile,
)

NOW = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=10)


def _mk_ws(root: Path, name: str, ws_name: str | None = None) -> Path:
    ws = root / name
    repo = ws / "app"
    repo.mkdir(parents=True)
    (repo / "Taskfile.yml").write_text("version: '3'\n")
    (ws / "mothership.yaml").write_text(
        f"workspace: {ws_name or name}\nrepos:\n  app:\n    path: app\n    type: service\n"
    )
    return ws


def _scan(root: Path):
    return scan_roots(DaemonConfig(scan_roots=[str(root)]))


def _store(home: Path) -> RegistryStore:
    return RegistryStore(registry_path(home))


def test_first_scan_mints_ids_and_writes_id_files(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    ws = _mk_ws(root, "a")
    bad = root / "bad"
    bad.mkdir()
    (bad / "mothership.yaml").write_text("workspace: [broken\n")
    state = reconcile(_store(home), _scan(root), NOW)
    healthy = next(e for e in state.entries if e.state == "healthy")
    degraded = next(e for e in state.entries if e.state == "degraded")
    assert (ws / ID_FILE_RELPATH).read_text().strip() == healthy.id
    assert not (bad / ID_FILE_RELPATH).exists()  # degraded: no id file written
    assert degraded.identity_source == "registry-only"


def test_rescan_idempotent(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    _mk_ws(root, "a")
    store = _store(home)
    s1 = reconcile(store, _scan(root), NOW)
    s2 = reconcile(store, _scan(root), LATER)
    assert len(s2.entries) == 1
    assert s2.entries[0].id == s1.entries[0].id
    assert s2.entries[0].last_seen == LATER


def test_move_preserves_identity_and_overrides(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    ws = _mk_ws(root, "a")
    store = _store(home)
    s1 = reconcile(store, _scan(root), NOW)
    original_id = s1.entries[0].id
    store.mutate(lambda s: setattr(s.entries[0], "ignored", True))
    moved = root / "renamed"
    shutil.move(str(ws), str(moved))
    s2 = reconcile(store, _scan(root), LATER)
    assert len(s2.entries) == 1
    e = s2.entries[0]
    assert e.id == original_id
    assert e.path == str(moved.resolve())
    assert e.ignored is True


def test_copy_surfaces_degraded_duplicate_both_orders(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    ws = _mk_ws(root, "a")
    store = _store(home)
    s1 = reconcile(store, _scan(root), NOW)
    original_id = s1.entries[0].id
    copy = root / "a-backup"
    shutil.copytree(ws, copy)
    cands = _scan(root)
    for ordering in (cands, list(reversed(cands))):
        s2 = reconcile(store, ordering, LATER)
        keeper = next(e for e in s2.entries if e.id == original_id)
        assert keeper.path == str(ws.resolve())  # existing path kept
        dup = next(e for e in s2.entries if "duplicate-identity" in e.detail)
        assert dup.state == "degraded"
        assert dup.path == str(copy.resolve())
        # copy's id file untouched (no rewrite)
        assert (copy / ID_FILE_RELPATH).read_text().strip() == original_id
        # reset the dup entries between orderings
        store.mutate(lambda s: setattr(s, "entries", [e for e in s.entries if "duplicate-identity" not in e.detail]))


def test_delete_degrades_to_missing(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    ws = _mk_ws(root, "a")
    store = _store(home)
    reconcile(store, _scan(root), NOW)
    shutil.rmtree(ws)
    s2 = reconcile(store, _scan(root), LATER)
    assert len(s2.entries) == 1
    assert s2.entries[0].state == "missing"
    assert "no longer exists" in s2.entries[0].detail


def test_id_file_unwritable_registers_registry_only(tmp_path: Path, monkeypatch):
    import mship.core.daemon.registry as reg_mod

    home, root = tmp_path / "home", tmp_path / "root"
    _mk_ws(root, "a")
    monkeypatch.setattr(reg_mod, "_write_id_file", lambda p, i: False)
    state = reconcile(_store(home), _scan(root), NOW)
    assert state.entries[0].state == "healthy"
    assert state.entries[0].identity_source == "registry-only"


def test_same_basename_same_display_name_two_entries(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    _mk_ws(root / "x", "proj", ws_name="proj")
    _mk_ws(root / "y", "proj", ws_name="proj")
    state = reconcile(_store(home), _scan(root), NOW)
    assert len(state.entries) == 2
    assert state.entries[0].id != state.entries[1].id
    assert all(e.state == "healthy" for e in state.entries)


def test_ignored_survives_rescan(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    _mk_ws(root, "a")
    store = _store(home)
    reconcile(store, _scan(root), NOW)
    store.mutate(lambda s: setattr(s.entries[0], "ignored", True))
    s2 = reconcile(store, _scan(root), LATER)
    assert s2.entries[0].ignored is True


def test_second_home_adopts_id_file_independently(tmp_path: Path):
    """Second registry (second host's home) sees the same id (from the id
    file) but is a fully independent entry — the two-hosts invariant at the
    reconcile layer."""
    root = tmp_path / "root"
    _mk_ws(root, "a")
    s_a = reconcile(_store(tmp_path / "home-a"), _scan(root), NOW)
    s_b = reconcile(_store(tmp_path / "home-b"), _scan(root), LATER)
    assert s_a.entries[0].id == s_b.entries[0].id  # adopted from the id file
    assert s_a.entries[0].last_seen != s_b.entries[0].last_seen  # independent


def test_degraded_candidate_updates_existing_entry(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    ws = _mk_ws(root, "a")
    store = _store(home)
    reconcile(store, _scan(root), NOW)
    (ws / "mothership.yaml").write_text("workspace: [broken\n")
    s2 = reconcile(store, _scan(root), LATER)
    assert len(s2.entries) == 1
    assert s2.entries[0].state == "degraded"
    assert "invalid" in s2.entries[0].detail


def test_duplicate_copy_does_not_accumulate_entries(tmp_path: Path):
    """Regression (#476 P2): the copy's id file still holds the ORIGINAL id, so
    every rescan re-resolved here and appended another degraded entry —
    workspaces.json grew without bound."""
    import shutil

    home, root = tmp_path / "home", tmp_path / "root"
    ws = _mk_ws(root, "a")
    store = _store(home)
    reconcile(store, _scan(root), NOW)
    shutil.copytree(ws, root / "a-copy")

    for _ in range(4):  # repeated daemon rescans
        state = reconcile(store, _scan(root), LATER)

    dups = [e for e in state.entries if "duplicate-identity" in e.detail]
    assert len(dups) == 1, [e.detail for e in state.entries]
    assert len(state.entries) == 2  # original + one degraded copy, stable
