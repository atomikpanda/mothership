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


class StreamingSubApp:
    """ASGI app that emits body chunks with gaps — proves the proxy streams
    rather than buffering to completion (the `/exec` iter_raw contract)."""

    def __init__(self):
        self.cancelled = False
        self.sent = 0

    async def __call__(self, scope, receive, send):
        import asyncio

        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        try:
            for i in range(3):
                await send({"type": "http.response.body", "body": f"chunk-{i}\n".encode(),
                            "more_body": True})
                self.sent += 1
                await asyncio.sleep(0.05)
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    @property
    def router(self):
        from contextlib import asynccontextmanager

        class _R:
            def lifespan_context(self, app):
                @asynccontextmanager
                async def cm():
                    yield
                return cm()

        return _R()


def test_forward_streams_chunks_incrementally(tmp_path):
    """Regression (#476 P2): a buffered proxy delivered nothing until the task
    exited, breaking live `mship ... --remote` output.

    Driven at the ASGI layer, not through TestClient: TestClient collects the
    whole body before `iter_raw()` yields, so it cannot distinguish streaming
    from buffering — it would pass against the buffered implementation too.
    """
    import asyncio

    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-s", "streamer", tmp_path / "s")])
    sub = StreamingSubApp()
    app = create_host_app(store, auth_token=None, build_subapp=lambda e, **kw: sub)

    async def drive():
        received: list[bytes] = []
        first_chunk_seen_at_sent: list[int] = []
        done = asyncio.Event()

        first = {"sent": False}

        async def receive():
            # Starlette's StreamingResponse runs a disconnect listener that
            # loops on receive(); without an eventual http.disconnect the task
            # group never exits and app() never returns.
            if not first["sent"]:
                first["sent"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    received.append(body)
                    if len(received) == 1:
                        first_chunk_seen_at_sent.append(sub.sent)
                if not message.get("more_body", False):
                    done.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "path": "/workspaces/ws-s/exec/run", "raw_path": b"/workspaces/ws-s/exec/run",
            "root_path": "", "scheme": "http", "query_string": b"", "headers": [],
            "client": ("test", 1), "server": ("test", 80),
        }
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), timeout=15)
        return received, first_chunk_seen_at_sent

    received, first_at = asyncio.run(drive())
    assert b"".join(received) == b"chunk-0\nchunk-1\nchunk-2\n"
    # the client had chunk 0 in hand before the sub-app had emitted all three
    assert first_at and first_at[0] < 3, f"response was buffered until completion (sent={first_at})"


def test_moved_workspace_rebuilds_subapp(tmp_path):
    """Regression (#476 P2): same id, new path — the cached sub-app pointed at
    the OLD workspace root/state dir until the daemon restarted."""
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-m", "mover", tmp_path / "before")])
    built: list[str] = []

    def build(entry, **kw):
        built.append(entry.path)
        return FakeSubApp(entry.name)

    app = create_host_app(store, auth_token=None, build_subapp=build)
    with TestClient(app) as client:
        client.get("/workspaces/ws-m/specs")
        assert built == [str(tmp_path / "before")]
        # reconciliation moved it (same id, new path)
        store.mutate(lambda s: setattr(s.entries[0], "path", str(tmp_path / "after")))
        client.get("/workspaces/ws-m/specs")
        assert built == [str(tmp_path / "before"), str(tmp_path / "after")]


def test_unbuildable_workspace_is_503_not_500(tmp_path):
    """A workspace the registry advertises but that won't build now must
    degrade with a reason, never surface an opaque 500."""
    from mship.core.workspace_context import ContextError

    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-x", "gone", tmp_path / "gone")])

    def build(entry, **kw):
        raise ContextError("no mothership.yaml at /gone/mothership.yaml")

    app = create_host_app(store, auth_token=None, build_subapp=build)
    with TestClient(app) as client:
        r = client.get("/workspaces/ws-x/specs")
        assert r.status_code == 503
        assert "no mothership.yaml" in r.json()["detail"]
