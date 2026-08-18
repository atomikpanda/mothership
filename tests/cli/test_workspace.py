"""`mship workspace ...` CLI (#472 Task 9)."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import mship.cli.workspace as ws_mod
from mship.core.daemon.discovery import scan_roots
from mship.core.daemon.paths import registry_path
from mship.core.daemon.registry import DaemonConfig, RegistryStore, reconcile, save_daemon_config

runner = CliRunner()
NOW = datetime(2026, 8, 17, 4, 0, tzinfo=timezone.utc)


def _mk_ws(root: Path, name: str) -> Path:
    ws = root / name
    repo = ws / "app"
    repo.mkdir(parents=True)
    (repo / "Taskfile.yml").write_text("version: '3'\n")
    (ws / "mothership.yaml").write_text(
        f"workspace: {name}\nrepos:\n  app:\n    path: app\n    type: service\n"
    )
    return ws


@pytest.fixture
def cli(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(ws_mod.Path, "home", classmethod(lambda cls: home))
    app = typer.Typer()
    ws_mod.register(app, lambda required=True: None)
    return app, home, tmp_path


def _seed_scan(home: Path, root: Path):
    save_daemon_config(home, DaemonConfig(scan_roots=[str(root)]))
    return reconcile(RegistryStore(registry_path(home)), scan_roots(DaemonConfig(scan_roots=[str(root)])), NOW)


def test_list_renders_all_states(cli):
    app, home, tmp = cli
    root = tmp / "root"
    _mk_ws(root, "good")
    bad = root / "bad"
    bad.mkdir()
    (bad / "mothership.yaml").write_text("workspace: [broken\n")
    _seed_scan(home, root)
    res = runner.invoke(app, ["workspace", "list"])
    assert res.exit_code == 0, res.output
    ws = json.loads(res.output)["workspaces"]
    states = {w["name"]: w["state"] for w in ws}
    assert states["good"] == "healthy"
    assert "degraded" in states.values()


def test_add_validates_and_registers_manual(cli):
    app, home, tmp = cli
    ws = _mk_ws(tmp / "elsewhere", "manual-ws")
    res = runner.invoke(app, ["workspace", "add", str(ws)])
    assert res.exit_code == 0, res.output
    entries = RegistryStore(registry_path(home)).load().entries
    assert entries[0].origin == "manual"
    assert entries[0].name == "manual-ws"

    res = runner.invoke(app, ["workspace", "add", str(tmp / "nope")])
    assert res.exit_code == 1


def test_add_rehydrates_existing_degraded_workspace(cli):
    app, home, tmp = cli
    workspace = _mk_ws(tmp / "elsewhere", "manual-ws")
    assert runner.invoke(
        app, ["workspace", "add", str(workspace)]
    ).exit_code == 0

    def degrade(state):
        entry = state.entries[0]
        entry.state = "degraded"
        entry.detail = "workspace no longer under configured scan roots"
        entry.name = "stale"
        entry.repos = []
        entry.ignored = True

    RegistryStore(registry_path(home)).mutate(degrade)
    result = runner.invoke(app, ["workspace", "add", str(workspace)])
    entry = RegistryStore(registry_path(home)).load().entries[0]

    assert result.exit_code == 0, result.output
    assert entry.origin == "manual"
    assert entry.ignored is False
    assert entry.state == "healthy"
    assert entry.detail == ""
    assert entry.name == "manual-ws"
    assert [repo.name for repo in entry.repos] == ["app"]


def test_add_repairs_registry_only_identity_before_move(cli):
    from mship.core.daemon.registry import (
        ID_FILE_RELPATH,
        WorkspaceEntry,
    )

    app, home, tmp = cli
    root = tmp / "elsewhere"
    workspace = _mk_ws(root, "manual-ws")
    store = RegistryStore(registry_path(home))
    workspace_id = "ws-registry-only"
    store.mutate(lambda state: state.entries.append(WorkspaceEntry(
        id=workspace_id,
        name="stale",
        path=str(workspace.resolve()),
        config_path=str(workspace / "mothership.yaml"),
        state="degraded",
        detail="workspace no longer under configured scan roots",
        identity_source="registry-only",
        first_seen=NOW,
        last_seen=NOW,
    )))

    result = runner.invoke(app, ["workspace", "add", str(workspace)])
    repaired = store.load().entries[0]

    assert result.exit_code == 0, result.output
    assert repaired.id == workspace_id
    assert repaired.identity_source == "idfile"
    assert (workspace / ID_FILE_RELPATH).read_text().strip() == workspace_id

    moved = root / "moved"
    workspace.rename(moved)
    state = reconcile(
        store,
        scan_roots(DaemonConfig(scan_roots=[str(root)])),
        NOW,
    )
    assert len(state.entries) == 1
    assert state.entries[0].id == workspace_id
    assert state.entries[0].path == str(moved.resolve())



def test_add_moved_manual_workspace_preserves_durable_identity(cli):
    from mship.core.daemon.registry import ID_FILE_RELPATH

    app, home, tmp = cli
    workspace = _mk_ws(tmp / "outside", "manual-ws")
    assert runner.invoke(
        app, ["workspace", "add", str(workspace)]
    ).exit_code == 0
    store = RegistryStore(registry_path(home))
    original_id = store.load().entries[0].id

    moved = tmp / "moved"
    workspace.rename(moved)
    result = runner.invoke(app, ["workspace", "add", str(moved)])
    entries = store.load().entries

    assert result.exit_code == 0, result.output
    assert len(entries) == 1
    assert entries[0].id == original_id
    assert entries[0].path == str(moved.resolve())
    assert (moved / ID_FILE_RELPATH).read_text().strip() == original_id


def test_add_exact_path_replacement_ignores_missing_history(cli):
    from mship.core.daemon.registry import ID_FILE_RELPATH, WorkspaceEntry

    app, home, tmp = cli
    workspace = _mk_ws(tmp / "outside", "replacement")
    store = RegistryStore(registry_path(home))
    store.mutate(lambda state: state.entries.extend([
        WorkspaceEntry(
            id="ws-history",
            name="deleted",
            path=str(workspace.resolve()),
            config_path=str(workspace / "mothership.yaml"),
            state="missing",
            detail="workspace was replaced at this path",
            first_seen=NOW,
            last_seen=NOW,
        ),
        WorkspaceEntry(
            id="ws-current",
            name="stale",
            path=str(workspace.resolve()),
            config_path=str(workspace / "mothership.yaml"),
            state="degraded",
            detail="workspace no longer under configured scan roots",
            identity_source="registry-only",
            first_seen=NOW,
            last_seen=NOW,
        ),
    ]))

    result = runner.invoke(app, ["workspace", "add", str(workspace)])
    by_id = {
        entry.id: entry
        for entry in store.load().entries
    }

    assert result.exit_code == 0, result.output
    assert by_id["ws-history"].state == "missing"
    assert by_id["ws-current"].state == "healthy"
    assert by_id["ws-current"].origin == "manual"
    assert (workspace / ID_FILE_RELPATH).read_text().strip() == "ws-current"


def test_add_known_move_displaces_registry_only_destination_owner(cli):
    from mship.core.daemon.registry import WorkspaceEntry

    app, home, tmp = cli
    workspace = _mk_ws(tmp / "outside", "manual-ws")
    assert runner.invoke(
        app, ["workspace", "add", str(workspace)]
    ).exit_code == 0
    store = RegistryStore(registry_path(home))
    workspace_id = store.load().entries[0].id
    destination = tmp / "destination"
    workspace.rename(destination)
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

    result = runner.invoke(app, ["workspace", "add", str(destination)])
    by_id = {
        entry.id: entry
        for entry in store.load().entries
    }

    assert result.exit_code == 0, result.output
    assert by_id[workspace_id].state == "healthy"
    assert by_id[workspace_id].path == str(destination.resolve())
    assert by_id["ws-destination-history"].state == "missing"


def test_add_duplicate_identity_copy_mints_fresh_id(cli):
    app, home, tmp = cli
    root = tmp / "root"
    ws = _mk_ws(root, "orig")
    state = _seed_scan(home, root)
    orig_id = state.entries[0].id
    copy = root / "orig-copy"
    shutil.copytree(ws, copy)
    # rescan surfaces the duplicate
    reconcile(RegistryStore(registry_path(home)), scan_roots(DaemonConfig(scan_roots=[str(root)])), NOW)
    res = runner.invoke(app, ["workspace", "add", str(copy)])
    assert res.exit_code == 0, res.output
    entries = RegistryStore(registry_path(home)).load().entries
    copy_entry = next(e for e in entries if e.path == str(copy.resolve()))
    assert copy_entry.id != orig_id
    assert copy_entry.origin == "manual"
    assert (copy / ".mothership" / "workspace-id").read_text().strip() == copy_entry.id


def test_offline_refresh_reports_malformed_config_without_mutation(cli):
    from mship.core.daemon.paths import daemon_config_path

    app, home, tmp = cli
    root = tmp / "root"
    _mk_ws(root, "existing")
    state = _seed_scan(home, root)
    workspace_id = state.entries[0].id
    daemon_config_path(home).write_text("scan_roots: [broken\n")

    result = runner.invoke(app, ["workspace", "refresh"])
    entry = RegistryStore(registry_path(home)).load().entries[0]

    assert result.exit_code == 1
    assert "invalid daemon config" in result.output
    assert entry.id == workspace_id
    assert entry.state == "healthy"


@pytest.mark.parametrize("stale_marker", [False, True])
def test_add_refuses_task_worktree(cli, stale_marker):
    from mship.core.workspace_marker import MARKER_NAME, write_marker

    app, home, tmp = cli
    real = _mk_ws(tmp, "real")
    hub = tmp / "hub"
    wt = hub / "repo"
    wt.mkdir(parents=True)
    (wt / "mothership.yaml").write_text("workspace: inherited\nrepos: {}\n")
    if stale_marker:
        (hub / MARKER_NAME).write_text(str(tmp / "deleted-workspace"))
    else:
        write_marker(hub, real)
    res = runner.invoke(app, ["workspace", "add", str(wt)])
    assert res.exit_code == 1
    assert "worktree" in res.output


def test_remove_and_ignore_by_id(cli):
    app, home, tmp = cli
    root = tmp / "root"
    _mk_ws(root, "a")
    state = _seed_scan(home, root)
    wid = state.entries[0].id
    assert runner.invoke(app, ["workspace", "ignore", wid]).exit_code == 0
    assert RegistryStore(registry_path(home)).load().entries[0].ignored is True
    assert runner.invoke(app, ["workspace", "remove", wid]).exit_code == 0
    assert RegistryStore(registry_path(home)).load().entries == []
    assert runner.invoke(app, ["workspace", "remove", "ws-nope"]).exit_code == 1


def test_ignore_and_remove_notify_live_daemon_cleanup(cli, monkeypatch):
    from mship.core.daemon.paths import lease_path

    app, home, tmp = cli
    root = tmp / "root"
    _mk_ws(root, "a")
    workspace_id = _seed_scan(home, root).entries[0].id
    lease = lease_path(home)
    lease.write_text('{"pid": 1, "socket_path": "/run/mship/daemon.sock"}')
    pokes = []
    monkeypatch.setattr(
        ws_mod,
        "_poke_daemon_refresh",
        lambda socket, **kwargs: pokes.append((socket, kwargs)),
    )

    assert runner.invoke(
        app, ["workspace", "ignore", workspace_id]
    ).exit_code == 0
    assert runner.invoke(
        app, ["workspace", "remove", workspace_id]
    ).exit_code == 0

    assert pokes == [
        ("/run/mship/daemon.sock", {"cleanup_only": True}),
        ("/run/mship/daemon.sock", {"cleanup_only": True}),
    ]


def test_refresh_direct_when_no_daemon(cli):
    app, home, tmp = cli
    root = tmp / "root"
    _mk_ws(root, "fresh")
    save_daemon_config(home, DaemonConfig(scan_roots=[str(root)]))
    res = runner.invoke(app, ["workspace", "refresh"])
    assert res.exit_code == 0, res.output
    assert len(RegistryStore(registry_path(home)).load().entries) == 1


def test_refresh_pokes_live_daemon(cli, monkeypatch):
    from mship.core.daemon.paths import lease_path

    app, home, tmp = cli
    lp = lease_path(home)
    lp.parent.mkdir(parents=True)
    lp.write_text('{"pid": 1, "socket_path": "/run/mship/daemon.sock"}')
    poked = {}
    monkeypatch.setattr(ws_mod, "_poke_daemon_refresh", lambda sock: poked.update(sock=sock) or {"workspaces": 3})
    res = runner.invoke(app, ["workspace", "refresh"])
    assert res.exit_code == 0, res.output
    assert poked["sock"] == "/run/mship/daemon.sock"
    assert "3" in res.output


def test_refresh_falls_back_offline_when_live_daemon_lacks_endpoint(
    cli, monkeypatch
):
    from mship.core.daemon.paths import lease_path

    app, home, tmp = cli
    root = tmp / "root"
    _mk_ws(root, "fresh")
    save_daemon_config(home, DaemonConfig(scan_roots=[str(root)]))
    lp = lease_path(home)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text('{"pid": 1, "socket_path": "/run/mship/daemon.sock"}')
    monkeypatch.setattr(
        ws_mod, "_poke_daemon_refresh", lambda _socket: None
    )

    res = runner.invoke(app, ["workspace", "refresh"])

    assert res.exit_code == 0, res.output
    assert "rescanned directly" in res.output
    assert len(RegistryStore(registry_path(home)).load().entries) == 1



def test_refresh_reports_live_daemon_rejection_without_offline_fallback(
    cli, monkeypatch
):
    from mship.core.daemon.paths import lease_path

    app, home, tmp = cli
    lp = lease_path(home)
    lp.parent.mkdir(parents=True)
    lp.write_text('{"pid": 1, "socket_path": "/run/mship/daemon.sock"}')
    offline_loads = []
    monkeypatch.setattr(
        ws_mod,
        "_poke_daemon_refresh",
        lambda _socket: {
            "error": "daemon refresh failed (503): /unmounted/workspaces"
        },
    )
    monkeypatch.setattr(
        ws_mod,
        "load_daemon_config",
        lambda _home: offline_loads.append(_home),
    )

    res = runner.invoke(app, ["workspace", "refresh"])

    assert res.exit_code == 1
    assert "/unmounted/workspaces" in res.output
    assert offline_loads == []
