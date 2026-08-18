"""AC-1 end-to-end (#472): install-seeded config → daemon startup scan →
registry → workspace-addressed serving, as ONE path. No `mship workspace add`
anywhere. Real sockets are the manual checklist's job; here uvicorn is
captured at the seam and the built apps are exercised directly, which pins
every hop the daemon owns (config → scan → reconcile → host app → per-id
forward) plus the TCP-bind wiring parameters."""
import shutil
from pathlib import Path

import pytest
import typer
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import mship.cli.daemon as daemon_mod
import mship.core.daemon.run as run_mod
from mship.core.daemon.paths import registry_path
from mship.core.daemon.registry import RegistryStore

runner = CliRunner()


def _mk_ws(root: Path, name: str) -> Path:
    ws = root / name
    repo = ws / "app"
    repo.mkdir(parents=True)
    (repo / "Taskfile.yml").write_text("version: '3'\n")
    (ws / "mothership.yaml").write_text(
        f"workspace: {name}\nrepos:\n  app:\n    path: app\n    type: service\n"
    )
    (ws / "specs").mkdir()
    return ws


def test_install_scan_serve_end_to_end(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "src"
    _mk_ws(root, "ws-one")
    _mk_ws(root, "ws-two")

    # 1) `mship daemon install --scan-root ... --serve ...` seeds the config.
    class _FakeSup:
        def available(self):
            return True

        def install(self, argv):
            pass

    monkeypatch.setattr(daemon_mod, "pick_supervisor", lambda **kw: _FakeSup())
    monkeypatch.setattr(daemon_mod, "resolve_mshipd_argv", lambda: ["/venv/bin/mshipd"])
    monkeypatch.setattr(daemon_mod.Path, "home", classmethod(lambda cls: home))
    app = typer.Typer()
    daemon_mod.register(app, lambda required=True: None)
    res = runner.invoke(app, [
        "daemon", "install", "--scan-root", str(root), "--serve", "127.0.0.1:47199",
    ])
    assert res.exit_code == 0, res.output

    # 2) daemon startup: capture what _serve_forever would bind, keep the apps.
    captured = {}
    monkeypatch.setattr(
        run_mod, "_serve_forever",
        lambda control_app, socket_path, host_app, serve_cfg: captured.update(
            control=control_app, host=host_app, serve=serve_cfg,
        ),
    )
    assert run_mod.main(home=home, env={}) == 0

    # startup scan discovered and registered both — no `mship workspace add`.
    entries = RegistryStore(registry_path(home)).load().entries
    assert sorted(e.name for e in entries) == ["ws-one", "ws-two"]
    assert all(e.state == "healthy" for e in entries)
    assert captured["serve"] == {"host": "127.0.0.1", "port": 47199}
    assert captured["host"] is not None

    # 3) the TCP host app serves BOTH workspaces, addressed by id.
    from mship.core.daemon.host_app import ensure_host_token

    token = ensure_host_token(home, env={})
    with TestClient(captured["host"]) as client:
        hdrs = {"Authorization": f"Bearer {token}"}
        ws = client.get("/workspaces", headers=hdrs).json()["workspaces"]
        assert sorted(w["name"] for w in ws) == ["ws-one", "ws-two"]
        for w in ws:
            r = client.get(f"/workspaces/{w['id']}/health", headers=hdrs)
            assert r.status_code == 200
            assert r.json()["workspace"] == w["name"]

    # 4) control app reports the flipped capabilities.
    with TestClient(captured["control"]) as client:
        caps = client.get("/health").json()["capabilities"]
        assert caps["registry"] is True and caps["serve"] is True


def test_refresh_rereads_daemon_config(tmp_path, monkeypatch):
    """Regression (#476 P1): rescan closed over the STARTUP config, so editing
    config.yaml and running `mship workspace refresh` scanned the old roots
    forever — defeating the documented edit-then-refresh workflow."""
    from mship.core.daemon.registry import DaemonConfig, save_daemon_config

    home = tmp_path / "home"
    home.mkdir()
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _mk_ws(root_a, "first")
    _mk_ws(root_b, "second")
    save_daemon_config(home, DaemonConfig(scan_roots=[str(root_a)]))

    captured = {}
    monkeypatch.setattr(
        run_mod, "_serve_forever",
        lambda control_app, socket_path, host_app, serve_cfg: captured.update(rescan=True),
    )
    store, rescan, _serve = run_mod._build_registry(home)
    assert sorted(e.name for e in store.load().entries) == ["first"]

    # operator edits the config, then refreshes (no daemon restart)
    save_daemon_config(home, DaemonConfig(scan_roots=[str(root_a), str(root_b)]))
    rescan()
    assert sorted(e.name for e in store.load().entries) == ["first", "second"]


def test_invalid_daemon_config_clears_previously_healthy_registry(tmp_path):
    from mship.core.daemon.paths import daemon_config_path
    from mship.core.daemon.registry import DaemonConfig, save_daemon_config

    home = tmp_path / "home"
    root = tmp_path / "root"
    _mk_ws(root, "existing")
    save_daemon_config(home, DaemonConfig(scan_roots=[str(root)]))
    store, _rescan, _serve = run_mod._build_registry(home)
    assert store.load().entries[0].state == "healthy"

    daemon_config_path(home).write_text("scan_roots: [broken\n")
    failed_store, _rescan, _serve = run_mod._build_registry(home)

    assert failed_store.load().entries == []


def test_initial_invalid_config_recovers_on_refresh_without_restart(tmp_path):
    from mship.core.daemon.paths import daemon_config_path
    from mship.core.daemon.registry import DaemonConfig, save_daemon_config

    home = tmp_path / "home"
    root = tmp_path / "root"
    _mk_ws(root, "recovered")
    config_path = daemon_config_path(home)
    config_path.parent.mkdir(parents=True)
    config_path.write_text("scan_roots: [broken\n")

    store, rescan, serve = run_mod._build_registry(home)
    assert store.load().entries == []
    assert serve is None

    save_daemon_config(home, DaemonConfig(scan_roots=[str(root)]))
    rescan()

    assert [entry.name for entry in store.load().entries] == ["recovered"]


def test_missing_scan_root_fails_without_mutating_healthy_registry(tmp_path):
    from mship.core.daemon.discovery import ScanRootError
    from mship.core.daemon.registry import DaemonConfig, save_daemon_config

    home = tmp_path / "home"
    root = tmp_path / "root"
    _mk_ws(root, "existing")
    save_daemon_config(home, DaemonConfig(scan_roots=[str(root)]))
    store, rescan, _serve = run_mod._build_registry(home)
    workspace_id = store.load().entries[0].id

    shutil.rmtree(root)
    for scan in (rescan, lambda: run_mod._build_registry(home)):
        with pytest.raises(ScanRootError, match=str(root)):
            scan()
        entry = store.load().entries[0]
        assert entry.id == workspace_id
        assert entry.state == "healthy"


def test_unreadable_config_fails_without_mutating_healthy_registry(
    tmp_path, monkeypatch
):
    from mship.core.daemon.paths import daemon_config_path
    from mship.core.daemon.registry import (
        DaemonConfig,
        DaemonConfigReadError,
        save_daemon_config,
    )

    home = tmp_path / "home"
    root = tmp_path / "root"
    _mk_ws(root, "existing")
    save_daemon_config(home, DaemonConfig(scan_roots=[str(root)]))
    store, rescan, _serve = run_mod._build_registry(home)
    workspace_id = store.load().entries[0].id
    config_path = daemon_config_path(home)
    real_read_text = Path.read_text

    def fail_config_read(self, *args, **kwargs):
        if self == config_path:
            raise PermissionError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_config_read)
    for scan in (rescan, lambda: run_mod._build_registry(home)):
        with pytest.raises(DaemonConfigReadError, match=str(config_path)):
            scan()
        entry = store.load().entries[0]
        assert entry.id == workspace_id
        assert entry.state == "healthy"
