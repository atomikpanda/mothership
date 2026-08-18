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


def test_deleted_original_does_not_promote_registered_duplicate(tmp_path: Path):
    """A known duplicate remains degraded until explicitly re-identified, even
    after the original disappears."""
    home, root = tmp_path / "home", tmp_path / "root"
    original = _mk_ws(root, "a")
    store = _store(home)
    first = reconcile(store, _scan(root), NOW)
    original_id = first.entries[0].id
    copy = root / "a-copy"
    shutil.copytree(original, copy)
    reconcile(store, _scan(root), LATER)

    shutil.rmtree(original)
    state = reconcile(store, _scan(root), LATER + timedelta(minutes=10))

    keeper = next(e for e in state.entries if e.id == original_id)
    duplicate = next(e for e in state.entries if e.path == str(copy.resolve()))
    assert keeper.path == str(original.resolve())
    assert keeper.state == "missing"
    assert duplicate.id != original_id
    assert duplicate.state == "degraded"
    assert "duplicate-identity" in duplicate.detail
    assert len({e.path for e in state.entries}) == len(state.entries)


def test_discovered_workspace_outside_scan_roots_is_not_healthy(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    workspace = _mk_ws(root, "a")
    store = _store(home)
    reconcile(store, _scan(root), NOW)

    state = reconcile(store, [], LATER)

    assert workspace.exists()
    assert state.entries[0].state == "degraded"
    assert "configured scan roots" in state.entries[0].detail


def test_recovered_registry_only_workspace_persists_identity(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    workspace = root / "a"
    workspace.mkdir(parents=True)
    (workspace / "mothership.yaml").write_text("workspace: [\n")
    store = _store(home)
    degraded = reconcile(store, _scan(root), NOW).entries[0]
    assert degraded.identity_source == "registry-only"
    assert not (workspace / ID_FILE_RELPATH).exists()

    app = workspace / "app"
    app.mkdir()
    (workspace / "mothership.yaml").write_text(
        "workspace: a\nrepos:\n  app:\n    path: app\n    type: service\n"
    )
    recovered = reconcile(store, _scan(root), LATER).entries[0]

    assert recovered.id == degraded.id
    assert recovered.identity_source == "idfile"
    assert (workspace / ID_FILE_RELPATH).read_text().strip() == degraded.id

    moved = root / "moved"
    workspace.rename(moved)
    after_move = reconcile(store, _scan(root), LATER + timedelta(minutes=10))
    assert len(after_move.entries) == 1
    assert after_move.entries[0].id == degraded.id
    assert after_move.entries[0].path == str(moved.resolve())


def test_recovered_registry_only_workspace_adopts_unknown_id_file(tmp_path: Path):
    """A degraded workspace copied from another host keeps that host's durable
    identity instead of replacing it with this registry's temporary id."""
    home, root = tmp_path / "home", tmp_path / "root"
    workspace = root / "a"
    workspace.mkdir(parents=True)
    (workspace / "mothership.yaml").write_text("workspace: [\n")
    id_file = workspace / ID_FILE_RELPATH
    id_file.parent.mkdir()
    id_file.write_text("ws-from-other-host\n")
    store = _store(home)

    degraded = reconcile(store, _scan(root), NOW).entries[0]
    assert degraded.identity_source == "registry-only"
    assert degraded.id != "ws-from-other-host"

    app = workspace / "app"
    app.mkdir()
    (workspace / "mothership.yaml").write_text(
        "workspace: a\nrepos:\n  app:\n    path: app\n    type: service\n"
    )
    recovered = reconcile(store, _scan(root), LATER).entries[0]

    assert recovered.id == "ws-from-other-host"
    assert recovered.identity_source == "idfile"
    assert id_file.read_text().strip() == "ws-from-other-host"


def test_state_dir_collision_uses_effective_resolver(tmp_path: Path, monkeypatch):
    import mship.core.workspace_context as context_mod

    home, root = tmp_path / "home", tmp_path / "root"
    _mk_ws(root, "a")
    _mk_ws(root, "b")
    shared = tmp_path / "effective-state"
    monkeypatch.setattr(context_mod, "_resolve_state_dir", lambda _path: shared)

    state = reconcile(_store(home), _scan(root), NOW)

    healthy = [e for e in state.entries if e.state == "healthy"]
    collisions = [e for e in state.entries if "state-dir collision" in e.detail]
    assert len(healthy) == 1
    assert len(collisions) == 1


def test_move_then_reuse_old_path_registers_both_workspaces(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    original = _mk_ws(root, "a")
    store = _store(home)
    original_id = reconcile(store, _scan(root), NOW).entries[0].id

    moved = root / "z"
    original.rename(moved)
    replacement = _mk_ws(root, "a", ws_name="replacement")
    state = reconcile(store, _scan(root), LATER)

    by_path = {e.path: e for e in state.entries}
    assert len(state.entries) == 2
    assert by_path[str(moved.resolve())].id == original_id
    assert by_path[str(replacement.resolve())].id != original_id
    assert by_path[str(replacement.resolve())].name == "replacement"


def test_manual_workspace_is_revalidated_outside_scan_roots(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "manual"
    workspace = _mk_ws(root, "workspace")
    store = _store(home)
    reconcile(store, _scan(root), NOW)
    store.mutate(lambda state: setattr(state.entries[0], "origin", "manual"))
    (workspace / "mothership.yaml").write_text("workspace: [broken\n")

    entry = reconcile(store, [], LATER).entries[0]

    assert entry.origin == "manual"
    assert entry.state == "degraded"
    assert "invalid" in entry.detail


def test_replacement_at_deleted_path_gets_new_identity_and_keeps_history(
    tmp_path: Path,
):
    home, root = tmp_path / "home", tmp_path / "root"
    workspace = _mk_ws(root, "workspace")
    store = _store(home)
    original_id = reconcile(store, _scan(root), NOW).entries[0].id

    shutil.rmtree(workspace)
    replacement = _mk_ws(root, "workspace", ws_name="replacement")
    state = reconcile(store, _scan(root), LATER)

    original = next(entry for entry in state.entries if entry.id == original_id)
    current = next(entry for entry in state.entries if entry.id != original_id)
    assert len(state.entries) == 2
    assert original.state == "missing"
    assert "replaced" in original.detail
    assert current.state == "healthy"
    assert current.name == "replacement"
    assert current.path == original.path == str(replacement.resolve())


def test_replacement_history_does_not_shadow_later_known_move(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    workspace_a = _mk_ws(root, "a")
    workspace_y = _mk_ws(root, "y")
    store = _store(home)
    initial = reconcile(store, _scan(root), NOW)
    id_a = next(entry.id for entry in initial.entries if entry.name == "a")
    id_y = next(entry.id for entry in initial.entries if entry.name == "y")

    shutil.rmtree(workspace_y)
    replacement = _mk_ws(root, "y", ws_name="replacement")
    replaced = reconcile(store, _scan(root), LATER)
    id_replacement = next(
        entry.id for entry in replaced.entries if entry.state == "healthy"
        and entry.path == str(replacement.resolve())
    )

    shutil.rmtree(replacement)
    workspace_a.rename(workspace_y)
    moved = reconcile(store, _scan(root), LATER + timedelta(minutes=1))

    by_id = {entry.id: entry for entry in moved.entries}
    assert by_id[id_a].state == "healthy"
    assert by_id[id_a].path == str(workspace_y.resolve())
    assert by_id[id_y].state == "missing"
    assert by_id[id_replacement].state == "missing"
    assert [entry.id for entry in moved.entries if entry.state == "healthy"] == [id_a]


def test_known_move_displaces_registry_only_destination_owner(tmp_path: Path):
    from mship.core.daemon.registry import WorkspaceEntry

    home, root = tmp_path / "home", tmp_path / "root"
    workspace = _mk_ws(root, "a")
    store = _store(home)
    workspace_id = reconcile(store, _scan(root), NOW).entries[0].id
    destination = root / "y"
    store.mutate(lambda state: state.entries.append(WorkspaceEntry(
        id="ws-destination-history",
        name="destination",
        path=str(destination.resolve()),
        config_path=str(destination / "mothership.yaml"),
        state="healthy",
        identity_source="registry-only",
        first_seen=NOW,
        last_seen=NOW,
    )))

    workspace.rename(destination)
    state = reconcile(store, _scan(root), LATER)
    by_id = {entry.id: entry for entry in state.entries}

    assert by_id[workspace_id].state == "healthy"
    assert by_id[workspace_id].path == str(destination.resolve())
    assert by_id["ws-destination-history"].state == "missing"
    assert "replaced" in by_id["ws-destination-history"].detail


def test_invalid_runner_degrades_only_its_workspace(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    _mk_ws(root, "healthy")
    malformed = _mk_ws(root, "malformed")
    with (malformed / "mothership.yaml").open("a") as config:
        config.write("runner: broken\n")

    state = reconcile(_store(home), _scan(root), NOW)
    by_name = {entry.name: entry for entry in state.entries}

    assert by_name["healthy"].state == "healthy"
    assert by_name["malformed"].state == "degraded"
    assert "runner" in by_name["malformed"].detail
