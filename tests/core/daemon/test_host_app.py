"""Workspace-addressed host app (#472 Task 7)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mship.core.daemon.host_app import create_host_app, ensure_host_token
from mship.core.daemon.paths import registry_path
from mship.core.daemon.registry import RegistryStore, RepoInfo, RuntimeInfo, WorkspaceEntry

NOW = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)


def _entry(id, name, path, state="healthy", detail="", **kw):
    return WorkspaceEntry(
        id=id, name=name, path=str(path), config_path=str(Path(path) / "mothership.yaml"),
        state=state, detail=detail, first_seen=NOW, last_seen=NOW, **kw,
    )


def _seed(home: Path, entries) -> RegistryStore:
    store = RegistryStore(registry_path(home))
    store.mutate(lambda s: s.entries.extend(entries))
    return store


class FakeSubApp:
    """Minimal ASGI app standing in for create_app: distinct data per
    workspace + a lifespan flag so we can pin that lifespans actually run."""

    def __init__(self, name):
        self.name = name
        self.lifespan_started = False
        self.lifespan_stopped = False

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":  # pragma: no cover - not used via router hack
            return
        body = f'{{"workspace": "{self.name}", "path": "{scope["path"]}"}}'.encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    @property
    def router(self):
        outer = self

        class _R:
            def lifespan_context(self, app):
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def cm():
                    outer.lifespan_started = True
                    try:
                        yield
                    finally:
                        outer.lifespan_stopped = True

                return cm()

        return _R()


@pytest.fixture
def two_ws_app(tmp_path):
    home = tmp_path / "home"
    meta = _entry("ws-meta", "meta", tmp_path / "meta",
                  repos=[RepoInfo(name="alpha", path="alpha"), RepoInfo(name="beta", path="beta")],
                  runtime=RuntimeInfo(venv_path="/v/meta"))
    mono = _entry("ws-mono", "mono", tmp_path / "mono",
                  repos=[RepoInfo(name="mono", path="mono"), RepoInfo(name="pkg", path="pkg", git_root="mono")],
                  runtime=RuntimeInfo(venv_path="/v/mono"))
    bad = _entry("ws-bad", "bad", tmp_path / "bad", state="degraded", detail="invalid yaml")
    store = _seed(home, [meta, mono, bad])
    built: dict[str, FakeSubApp] = {}

    def build(entry, *, auth_token, pr_watch_interval):
        sub = FakeSubApp(entry.name)
        built[entry.id] = sub
        return sub

    app = create_host_app(store, auth_token=None, build_subapp=build)
    return app, store, built


def test_list_workspaces_includes_all_states(two_ws_app):
    app, store, built = two_ws_app
    with TestClient(app) as client:
        r = client.get("/workspaces")
        assert r.status_code == 200
        ws = {w["id"]: w for w in r.json()["workspaces"]}
        assert set(ws) == {"ws-meta", "ws-mono", "ws-bad"}
        assert ws["ws-bad"]["state"] == "degraded"
        assert ws["ws-meta"]["runtime"]["venv_path"] == "/v/meta"
        assert ws["ws-mono"]["repos"][1]["git_root"] == "mono"


def test_forward_routes_to_right_workspace_no_cross_bleed(two_ws_app):
    app, store, built = two_ws_app
    with TestClient(app) as client:
        r1 = client.get("/workspaces/ws-meta/specs")
        r2 = client.get("/workspaces/ws-mono/specs")
        assert r1.json() == {"workspace": "meta", "path": "/specs"}
        assert r2.json() == {"workspace": "mono", "path": "/specs"}


def test_subapp_lifespans_actually_start_and_stop(two_ws_app):
    """The mounted-lifespan gotcha, pinned: forwarding must run each sub-app's
    lifespan (a plain app.mount would silently skip PrWatcher startup)."""
    app, store, built = two_ws_app
    with TestClient(app) as client:
        client.get("/workspaces/ws-meta/specs")
        assert built["ws-meta"].lifespan_started is True
        assert built["ws-meta"].lifespan_stopped is False
    assert built["ws-meta"].lifespan_stopped is True  # host shutdown stops sub-apps


def test_unknown_id_404_degraded_503(two_ws_app):
    app, store, built = two_ws_app
    with TestClient(app) as client:
        assert client.get("/workspaces/nope/specs").status_code == 404
        r = client.get("/workspaces/ws-bad/specs")
        assert r.status_code == 503
        assert "invalid yaml" in r.json()["detail"]
        assert "ws-bad" not in built  # degraded entries never build a sub-app


def test_refresh_adds_and_removes_without_reconstruction(two_ws_app, tmp_path):
    app, store, built = two_ws_app
    with TestClient(app) as client:
        client.get("/workspaces/ws-meta/specs")

        def swap(s):
            s.entries = [e for e in s.entries if e.id != "ws-meta"]
            s.entries.append(_entry("ws-new", "newws", tmp_path / "new"))

        store.mutate(swap)
        r = client.post("/workspaces/refresh")
        assert r.status_code == 200
        assert built["ws-meta"].lifespan_stopped is True  # removed → watcher stopped
        assert client.get("/workspaces/ws-new/specs").json()["workspace"] == "newws"
        assert client.get("/workspaces/ws-meta/specs").status_code == 404


def test_host_token_gates_everything(tmp_path):
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])
    app = create_host_app(store, auth_token="sekrit", build_subapp=lambda e, **kw: FakeSubApp(e.name))
    with TestClient(app) as client:
        assert client.get("/workspaces").status_code == 401
        assert client.get("/workspaces", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = client.get("/workspaces", headers={"Authorization": "Bearer sekrit"})
        assert ok.status_code == 200
        assert client.get("/workspaces/ws-a/specs", headers={"Authorization": "Bearer sekrit"}).status_code == 200


def test_ignored_entries_hidden_and_unroutable(tmp_path):
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-i", "i", tmp_path / "i", ignored=True)])
    app = create_host_app(store, auth_token=None, build_subapp=lambda e, **kw: FakeSubApp(e.name))
    with TestClient(app) as client:
        assert client.get("/workspaces").json()["workspaces"] == []
        assert client.get("/workspaces/ws-i/specs").status_code == 404


def test_ensure_host_token_stable(tmp_path):
    t1 = ensure_host_token(tmp_path)
    t2 = ensure_host_token(tmp_path)
    assert t1 == t2 and len(t1) > 20
