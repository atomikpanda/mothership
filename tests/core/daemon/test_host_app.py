"""Workspace-addressed host app (#472 Task 7)."""
import asyncio
import gc
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mship.core.daemon.host_app import create_host_app, ensure_host_token
from mship.core.daemon.discovery import ScanRootError
from mship.core.daemon.paths import registry_path
from mship.core.daemon.registry import RegistryReadError, RegistryStore, RepoInfo, RuntimeInfo, WorkspaceEntry

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
        self.seen_authorization = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":  # pragma: no cover - not used via router hack
            return
        self.seen_authorization = next(
            (v.decode() for k, v in scope["headers"] if k.lower() == b"authorization"),
            None,
        )
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

    def build(entry, *, auth_token, pr_watch_interval, **_credentials):
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


def test_health_count_and_list_distinguish_degraded_from_missing(tmp_path):
    home = tmp_path / "home"
    store = _seed(
        home,
        [
            _entry("ws-healthy", "healthy", tmp_path / "healthy"),
            _entry(
                "ws-degraded",
                "degraded",
                tmp_path / "degraded",
                state="degraded",
            ),
            _entry(
                "ws-missing",
                "missing",
                tmp_path / "missing",
                state="missing",
            ),
        ],
    )
    app = create_host_app(
        store,
        auth_token=None,
        build_subapp=lambda entry, **kwargs: FakeSubApp(entry.name),
    )

    with TestClient(app) as client:
        health = client.get("/health").json()
        assert (health["status"], health["workspaces"], health["degraded"]) == (
            "ok",
            3,
            1,
        )
        workspaces = client.get("/workspaces").json()["workspaces"]

    assert {workspace["state"] for workspace in workspaces} == {
        "healthy",
        "degraded",
        "missing",
    }


def test_forward_routes_to_right_workspace_no_cross_bleed(two_ws_app):
    app, store, built = two_ws_app
    with TestClient(app) as client:
        r1 = client.get("/workspaces/ws-meta/specs")
        r2 = client.get("/workspaces/ws-mono/specs")
        assert r1.json() == {
            "workspace": "meta",
            "path": "/workspaces/ws-meta/specs",
        }

        assert r2.json() == {
            "workspace": "mono",
            "path": "/workspaces/ws-mono/specs",
        }

def test_default_workspace_subapp_routes_under_host_namespace(tmp_path):
    workspace = tmp_path / "actual"
    workspace.mkdir()
    (workspace / "mothership.yaml").write_text(
        "workspace: actual\nrepos: {}\n"
    )
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-actual", "actual", workspace)])
    app = create_host_app(
        store,
        auth_token=None,
        pr_watch_interval=0,
    )

    with TestClient(app) as client:
        assert client.get("/workspaces/ws-actual/health").status_code == 200
        assert client.get("/workspaces/ws-actual/specs").status_code == 200
        redirect = client.get(
            "/workspaces/ws-actual/ui", follow_redirects=False
        )
        assert redirect.status_code == 307
        assert (
            redirect.headers["location"]
            == "http://testserver/workspaces/ws-actual/ui/"
        )
        assert client.get("/workspaces/ws-actual/ui/").status_code == 200


def test_default_workspace_subapp_ui_uses_host_cookie_flow(tmp_path):
    workspace = tmp_path / "actual"
    workspace.mkdir()
    (workspace / "mothership.yaml").write_text(
        "workspace: actual\nrepos: {}\n"
    )
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-actual", "actual", workspace)])
    app = create_host_app(
        store,
        auth_token="sekrit",
        pr_watch_interval=0,
    )
    ui_root = "/workspaces/ws-actual/ui"

    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/workspaces/ws-actual/health").status_code == 401
        assert client.get(
            "/workspaces/ws-actual/health",
            headers={"Authorization": "Bearer sekrit"},
        ).status_code == 200

        exchange = client.get(
            f"{ui_root}/?token=sekrit",
            follow_redirects=False,
        )
        assert exchange.status_code == 303
        assert exchange.headers["location"] == f"{ui_root}/"
        assert f"Path={ui_root}" in exchange.headers["set-cookie"]
        assert client.get(exchange.headers["location"]).status_code == 200



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
        assert client.get("/openapi.json").status_code == 404
        ok = client.get("/workspaces", headers={"Authorization": "Bearer sekrit"})
        assert ok.status_code == 200
        assert client.get("/workspaces/ws-a/specs", headers={"Authorization": "Bearer sekrit"}).status_code == 200


def test_workspace_ui_keeps_host_namespace_for_links_assets_and_cookie(
    tmp_path,
):
    from fastapi import FastAPI

    from mship.webui import mount_webui

    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])

    def build(_entry, *, auth_token, **_kwargs):
        subapp = FastAPI()
        mount_webui(
            subapp,
            payload_source=lambda: {
                "workspace": "a",
                "edges": [],
                "mship_version": "test",
                "probed_at": "now",
            },
            auth_token=auth_token,
        )
        return subapp

    app = create_host_app(
        store, auth_token="sekrit", build_subapp=build
    )
    ui_root = "/workspaces/ws-a/ui"
    with TestClient(app, base_url="https://testserver") as client:
        exchange = client.get(
            f"{ui_root}/?token=sekrit",
            follow_redirects=False,
        )
        assert exchange.status_code == 303
        assert exchange.headers["location"] == f"{ui_root}/"
        assert f"Path={ui_root}" in exchange.headers["set-cookie"]

        html = client.get(exchange.headers["location"])
        assert html.status_code == 200
        assert f'href="{ui_root}/static/app.css"' in html.text
        assert f'href="{ui_root}/doctor"' in html.text
        assert client.get(f"{ui_root}/static/app.css").status_code == 200
        assert client.get(f"{ui_root}/doctor").status_code == 200


@pytest.mark.parametrize(
    ("error_type", "detail"),
    [
        (ScanRootError, "/unmounted/workspaces is unavailable"),
        (RegistryReadError, "/registry/workspaces.json is unreadable"),
    ],
)
def test_host_refresh_reports_operational_error_without_dropping_cached_subapp(
    tmp_path, error_type, detail
):
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])
    built = {}

    def build(entry, **_kwargs):
        subapp = FakeSubApp(entry.name)
        built[entry.id] = subapp
        return subapp

    def fail_rescan():
        raise error_type(detail)

    app = create_host_app(
        store,
        auth_token=None,
        build_subapp=build,
        rescan=fail_rescan,
    )
    with TestClient(app) as client:
        assert client.get("/workspaces/ws-a/specs").status_code == 200
        cached = built["ws-a"]

        response = client.post("/workspaces/refresh")

        assert response.status_code == 503
        assert detail in response.json()["detail"]
        assert cached.lifespan_stopped is False
        assert store.load().entries[0].state == "healthy"
        assert client.get("/workspaces/ws-a/specs").status_code == 200
        assert built["ws-a"] is cached


def test_host_refresh_does_not_depend_on_default_executor_capacity(tmp_path):
    """A busy unrelated executor must not prevent a registry refresh."""
    import asyncio
    import concurrent.futures
    import threading

    import anyio
    import httpx

    occupied = threading.Event()
    release = threading.Event()
    rescanned = threading.Event()

    def occupy_default_executor():
        occupied.set()
        assert release.wait(3), "test did not release the default executor"

    store = _seed(tmp_path / "home", [])
    app = create_host_app(store, auth_token=None, rescan=rescanned.set)

    async def scenario():
        asyncio.get_running_loop().set_default_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=1)
        )
        blocker = asyncio.create_task(asyncio.to_thread(occupy_default_executor))
        try:
            with anyio.fail_after(1):
                while not occupied.is_set():
                    await anyio.sleep(0)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://host",
                ) as client:
                    response = await client.post("/workspaces/refresh")
            assert response.status_code == 200
            assert rescanned.is_set()
        finally:
            release.set()
            await blocker

    anyio.run(scenario, backend="asyncio")


def test_host_passes_github_app_credentials_to_workspace_subapp(tmp_path):
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])
    captured = {}

    def build(entry, **kwargs):
        captured.update(kwargs)
        return FakeSubApp(entry.name)

    app = create_host_app(
        store,
        auth_token=None,
        gh_app_id="123",
        gh_app_key="PRIVATE KEY",
        build_subapp=build,
    )
    with TestClient(app) as client:
        assert client.get("/workspaces/ws-a/specs").status_code == 200

    assert captured["gh_app_id"] == "123"
    assert captured["gh_app_key"] == "PRIVATE KEY"


@pytest.mark.parametrize("ambient_interval", ["0", "not-a-number"])
def test_daemon_host_passes_explicit_default_watch_interval(
    tmp_path, monkeypatch, ambient_interval
):
    from mship.core.serve import PR_WATCH_INTERVAL_SECONDS

    monkeypatch.setenv("MSHIP_PR_WATCH_INTERVAL", ambient_interval)
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])
    captured = {}

    def build(entry, **kwargs):
        captured.update(kwargs)
        return FakeSubApp(entry.name)

    app = create_host_app(
        store,
        auth_token=None,
        build_subapp=build,
    )
    with TestClient(app) as client:
        assert client.get("/workspaces/ws-a/specs").status_code == 200

    assert captured["pr_watch_interval"] == PR_WATCH_INTERVAL_SECONDS


def test_ignored_entries_hidden_and_unroutable(tmp_path):
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-i", "i", tmp_path / "i", ignored=True)])
    app = create_host_app(store, auth_token=None, build_subapp=lambda e, **kw: FakeSubApp(e.name))
    with TestClient(app) as client:
        assert client.get("/workspaces").json()["workspaces"] == []
        assert client.get("/workspaces/ws-i/specs").status_code == 404


def test_ensure_host_token_stable(tmp_path):
    t1 = ensure_host_token(tmp_path, env={})
    t2 = ensure_host_token(tmp_path, env={})
    assert t1 == t2 and len(t1) > 20


def test_ensure_host_token_prefers_environment(tmp_path):
    persisted = ensure_host_token(tmp_path, env={})

    assert ensure_host_token(
        tmp_path, env={"MSHIP_SERVE_TOKEN": "configured-token"}
    ) == "configured-token"
    assert persisted != "configured-token"


def test_ensure_host_token_canonicalizes_environment_override(tmp_path):
    from mship.core.daemon.paths import daemon_state_dir

    assert ensure_host_token(
        tmp_path, env={"MSHIP_SERVE_TOKEN": "  canonical-token \n"}
    ) == "canonical-token"
    assert not (daemon_state_dir(tmp_path) / "serve-token").exists()


def test_ensure_host_token_rejects_blank_environment_override(tmp_path):
    from mship.core.daemon.paths import daemon_state_dir

    with pytest.raises(ValueError, match="must not be blank"):
        ensure_host_token(tmp_path, env={"MSHIP_SERVE_TOKEN": " \t\n"})
    assert not (daemon_state_dir(tmp_path) / "serve-token").exists()


def test_persist_host_token_preserves_live_token_when_replace_fails(
    tmp_path, monkeypatch
):
    import mship.core.daemon.host_app as host_mod
    from mship.core.daemon.paths import daemon_state_dir

    persist_host_token = host_mod.persist_host_token
    persist_host_token(tmp_path, "previous")
    path = daemon_state_dir(tmp_path) / "serve-token"

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(host_mod.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        persist_host_token(tmp_path, "replacement")

    assert path.read_text().strip() == "previous"
    assert list(path.parent.glob("serve-token.*")) == []


def test_ensure_host_token_read_error_does_not_rotate_token(
    tmp_path, monkeypatch
):
    import mship.core.daemon.host_app as host_mod
    from mship.core.daemon.paths import daemon_state_dir

    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)
    host_mod.persist_host_token(tmp_path, "previous")
    path = daemon_state_dir(tmp_path) / "serve-token"
    real_read_text = Path.read_text

    def fail_token_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_token_read)
    with pytest.raises(RuntimeError, match=str(path)):
        host_mod.ensure_host_token(tmp_path, env={})

    assert path.read_bytes() == b"previous\n"


def test_persisted_github_app_read_error_names_owner_without_mutation(
    tmp_path, monkeypatch
):
    import mship.core.daemon.host_app as host_mod

    host_mod.persist_gh_app_credentials(tmp_path, "123", "PRIVATE KEY")
    _token_path, _app_id_path, app_key_path = host_mod._credential_paths(
        tmp_path
    )
    previous = app_key_path.read_bytes()
    real_read_text = Path.read_text

    def fail_key_read(self, *args, **kwargs):
        if self == app_key_path:
            raise PermissionError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_key_read)
    with pytest.raises(ValueError, match=str(app_key_path)):
        host_mod.load_gh_app_credentials(tmp_path, env={})

    assert app_key_path.read_bytes() == previous


def test_github_app_loader_rejects_blank_private_key(tmp_path):
    from mship.core.daemon.host_app import load_gh_app_credentials

    key_path = tmp_path / "blank.pem"
    key_path.write_text("  \n\t")

    with pytest.raises(ValueError, match=str(key_path)):
        load_gh_app_credentials(env={
            "MSHIP_GH_APP_ID": "123",
            "MSHIP_GH_APP_KEY": str(key_path),
        })


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


class _OwnedForwardSubApp:
    """An event-driven ASGI peer for request-owned forwarding tests."""

    def __init__(self, mode):
        self.mode = mode
        self.active: set[asyncio.Task] = set()
        self.entered = asyncio.Event()
        self.finalized = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.blocked_on_ninth_send = asyncio.Event()
        self.release = asyncio.Event()
        self.lifespan_stopped = asyncio.Event()

    async def __call__(self, _scope, _receive, send):
        task = asyncio.current_task()
        assert task is not None
        self.active.add(task)
        self.entered.set()
        try:
            if self.mode == "fail-before-start":
                raise RuntimeError("before response start")

            await send(
                {
                    "type": "http.response.start",
                    "status": 207,
                    "headers": [(b"x-forwarded-test", b"kept")],
                }
            )
            if self.mode == "fail-after-start":
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"raw-first\\x00",
                        "more_body": True,
                    }
                )
                raise RuntimeError("after response start")
            if self.mode == "block-on-ninth-send":
                for index in range(9):
                    if index == 8:
                        self.blocked_on_ninth_send.set()
                    await send(
                        {
                            "type": "http.response.body",
                            "body": bytes([index]),
                            "more_body": True,
                        }
                    )
                await send(
                    {"type": "http.response.body", "body": b"", "more_body": False}
                )
                return

            await send(
                {
                    "type": "http.response.body",
                    "body": b"raw-first\x00",
                    "more_body": True,
                }
            )
            if self.mode == "normal":
                await send(
                    {"type": "http.response.body", "body": b"raw-last", "more_body": False}
                )
            else:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.active.discard(task)
            self.finalized.set()

    @property
    def router(self):
        from contextlib import asynccontextmanager

        outer = self

        class _Router:
            def lifespan_context(self, _app):
                @asynccontextmanager
                async def lifespan():
                    try:
                        yield
                    finally:
                        outer.lifespan_stopped.set()

                return lifespan()

        return _Router()


def _owned_forward_app(tmp_path, mode):
    store = _seed(tmp_path / "home", [_entry("ws-stream", "stream", tmp_path / "stream")])
    subapp = _OwnedForwardSubApp(mode)
    app = create_host_app(
        store, auth_token=None, build_subapp=lambda _entry, **_kwargs: subapp
    )
    return app, subapp


def _forward_scope():
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/workspaces/ws-stream/exec/run",
        "raw_path": b"/workspaces/ws-stream/exec/run",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 80),
    }


def _forward_endpoint(app):
    def find(routes):
        for route in routes:
            if getattr(route, "name", None) == "forward":
                return route.endpoint
            nested = getattr(route, "routes", None)
            if nested is None:
                nested = getattr(getattr(route, "original_router", None), "routes", None)
            if nested:
                endpoint = find(nested)
                if endpoint is not None:
                    return endpoint
        return None

    endpoint = find(app.routes)
    assert endpoint is not None
    return endpoint


async def _wait_for(event):
    async with asyncio.timeout(5):
        await event.wait()


def test_forward_normal_completion_finalizes_request_producer_and_keeps_wire_shape(tmp_path):
    """Catches removing the terminal queue sentinel or losing forwarded status,
    headers, or raw chunks after the producer completes normally."""
    app, subapp = _owned_forward_app(tmp_path, "normal")

    async def scenario():
        messages = []
        response_finished = asyncio.Event()
        sent_request = False

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await response_finished.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                response_finished.set()

        async with app.router.lifespan_context(app):
            await app(_forward_scope(), receive, send)
        await _wait_for(subapp.finalized)
        return messages

    messages = asyncio.run(scenario())
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = [message.get("body", b"") for message in messages if message["type"] == "http.response.body"]
    assert (start["status"], dict(start["headers"])[b"x-forwarded-test"]) == (207, b"kept")
    assert body == [b"raw-first\x00", b"raw-last", b""]
    assert subapp.active == set()


def test_forward_client_disconnect_finalizes_request_producer(tmp_path):
    """Catches deleting ``task.cancel()`` from ``body_stream``'s disconnect
    finalizer, which leaves the workspace producer live after the client leaves."""
    app, subapp = _owned_forward_app(tmp_path, "wait-for-cancellation")

    async def scenario():
        first_body_forwarded = asyncio.Event()
        sent_request = False

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await first_body_forwarded.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                first_body_forwarded.set()

        async with app.router.lifespan_context(app):
            await app(_forward_scope(), receive, send)
        await _wait_for(subapp.finalized)

    asyncio.run(scenario())
    assert subapp.cancelled.is_set()
    assert subapp.active == set()


@pytest.mark.parametrize("mode", ["fail-before-start", "fail-after-start"])
def test_forward_subapp_failure_is_retrieved_and_logged_after_request_completion(
    tmp_path, caplog, mode
):
    """Catches silently swallowing a producer exception after retrieving it;
    failures before and after response start must reach the daemon log."""
    app, subapp = _owned_forward_app(tmp_path, mode)
    caplog.set_level(logging.ERROR, logger="mship.core.daemon.host_app")

    async def scenario():
        loop = asyncio.get_running_loop()
        unhandled = []
        previous_handler = loop.get_exception_handler()
        response_finished = asyncio.Event()
        sent_request = False

        def capture_unhandled(_loop, context):
            unhandled.append(context)

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await response_finished.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                response_finished.set()

        loop.set_exception_handler(capture_unhandled)
        try:
            async with app.router.lifespan_context(app):
                await app(_forward_scope(), receive, send)
            await _wait_for(subapp.finalized)
            gc.collect()
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)
        return unhandled

    unhandled = asyncio.run(scenario())
    assert not unhandled
    assert subapp.active == set()
    assert "forwarded workspace producer failed" in caplog.text


def test_forward_response_start_send_failure_finalizes_request_producer(tmp_path):
    """Catches returning a bare StreamingResponse: if its response-start send
    fails before body iteration, its producer must still be cancelled and joined."""
    app, subapp = _owned_forward_app(tmp_path, "wait-for-cancellation")

    async def scenario():
        sent_request = False

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Future()

        async def send(message):
            if message["type"] == "http.response.start":
                raise OSError("client disconnected while receiving response headers")

        async with app.router.lifespan_context(app):
            with pytest.raises(OSError, match="response headers"):
                await app(_forward_scope(), receive, send)
            try:
                assert subapp.cancelled.is_set()
                assert subapp.active == set()
            finally:
                # Release the peer if the assertion fails so a RED run leaves
                # no task behind when asyncio.run closes its loop.
                subapp.release.set()
                await _wait_for(subapp.finalized)

    asyncio.run(scenario())


def test_forward_cancellation_while_eight_chunk_queue_send_is_blocked_finalizes_producer(tmp_path):
    """Catches changing the eight-chunk queue to unbounded, or cancelling a
    blocked producer without draining it through the response lifecycle."""
    from starlette.requests import Request

    app, subapp = _owned_forward_app(tmp_path, "block-on-ninth-send")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def scenario():
        request = Request(_forward_scope(), receive=receive)
        async with app.router.lifespan_context(app):
            response = await _forward_endpoint(app)("ws-stream", "exec/run", request)
            await _wait_for(subapp.blocked_on_ninth_send)
            first = await anext(response.body_iterator)
            assert first == b"\x00"
            await response.body_iterator.aclose()
            await _wait_for(subapp.finalized)

    asyncio.run(scenario())
    assert subapp.cancelled.is_set()
    assert subapp.active == set()


def test_forward_connection_cancellation_before_body_iteration_finalizes_producer(tmp_path):
    """Catches cancellation while the response header send is blocked, before
    body iteration can enter the generator finalizer and settle its producer."""
    app, subapp = _owned_forward_app(tmp_path, "wait-for-cancellation")

    async def scenario():
        sent_request = False
        response_start_entered = asyncio.Event()
        hold_response_start = asyncio.Event()

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Future()

        async def send(message):
            if message["type"] == "http.response.start":
                response_start_entered.set()
                await hold_response_start.wait()

        async with app.router.lifespan_context(app):
            connection = asyncio.create_task(app(_forward_scope(), receive, send))
            await _wait_for(response_start_entered)
            connection.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connection
            try:
                assert subapp.cancelled.is_set()
                assert subapp.active == set()
            finally:
                subapp.release.set()
                await _wait_for(subapp.finalized)

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_mode", ["cancel", "header-error"])
def test_full_forward_queue_settles_before_any_body_iteration(tmp_path, exit_mode):
    """Header delivery failure/cancellation must join a full-queue producer."""
    app, subapp = _owned_forward_app(tmp_path, "block-on-ninth-send")

    async def scenario():
        header_entered = asyncio.Event()
        fail_headers = asyncio.Event()
        body_messages = []

        async def receive():
            await asyncio.Future()

        async def send(message):
            if message["type"] == "http.response.start":
                header_entered.set()
                await fail_headers.wait()
                raise OSError("header delivery failed")
            body_messages.append(message)

        async with app.router.lifespan_context(app):
            connection = asyncio.create_task(app(_forward_scope(), receive, send))
            try:
                await _wait_for(header_entered)
                await _wait_for(subapp.blocked_on_ninth_send)
                if exit_mode == "cancel":
                    connection.cancel()
                else:
                    fail_headers.set()
                done, _ = await asyncio.wait({connection}, timeout=1)
                assert done, "forward cleanup blocked on a full queue without a consumer"
                expected = asyncio.CancelledError if exit_mode == "cancel" else OSError
                with pytest.raises(expected):
                    await connection
                assert subapp.cancelled.is_set()
                assert subapp.active == set()
                assert body_messages == []
            finally:
                # A second cancellation releases the buggy final sentinel put
                # on RED, so this bounded regression leaves no pending task.
                connection.cancel()
                await asyncio.gather(connection, return_exceptions=True)

    asyncio.run(scenario())


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


def test_active_forward_stream_host_shutdown_joins_producer_and_subapp(
    tmp_path, monkeypatch
):
    """Real server shutdown must settle the request before ending its subapp."""
    from contextlib import asynccontextmanager
    from tempfile import TemporaryDirectory

    import anyio
    import httpx
    import uvicorn
    from fastapi import FastAPI

    from mship.core.daemon import run as run_mod

    async def scenario():
        host, subapp = _owned_forward_app(tmp_path, "wait-for-cancellation")
        control = FastAPI()
        ready = anyio.Event()
        stopped = anyio.Event()
        lifespan_ended = anyio.Event()
        control.state.set_serve_bound = lambda bound: ready.set() if bound else None
        original_lifespan = host.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            async with original_lifespan(app):
                yield
            assert subapp.active == set()
            assert subapp.finalized.is_set()
            assert subapp.lifespan_stopped.is_set()
            lifespan_ended.set()

        host.router.lifespan_context = lifespan
        servers = []
        real_server = uvicorn.Server

        def build_server(config):
            # The deliberately endless stream makes graceful shutdown expire;
            # Uvicorn must cancel and join the request before closing lifespan.
            config.timeout_graceful_shutdown = 0
            server = real_server(config)
            servers.append(server)
            return server

        monkeypatch.setattr(uvicorn, "Server", build_server)
        monkeypatch.setattr(run_mod, "_install_stop_handlers", lambda *_args: None)
        daemon_scope = anyio.CancelScope()

        async def daemon():
            with daemon_scope:
                await run_mod._serve(
                    control, Path(socket_dir) / "control.sock", host,
                    {"host": "127.0.0.1", "port": 0}, None,
                )
            stopped.set()

        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(daemon)
                await ready.wait()
                port = servers[1].servers[0].sockets[0].getsockname()[1]
                try:
                    async with httpx.AsyncClient() as client:
                        async with client.stream(
                            "POST", f"http://127.0.0.1:{port}/workspaces/ws-stream/exec/run"
                        ) as response:
                            assert response.status_code == 207
                            assert await anext(response.aiter_bytes()) == b"raw-first\x00"
                            assert subapp.active
                            daemon_scope.cancel()
                            await stopped.wait()
                finally:
                    daemon_scope.cancel()
                    subapp.release.set()
                assert lifespan_ended.is_set()
                assert subapp.cancelled.is_set()
                assert all(not server.server_state.tasks for server in servers)

    # macOS pytest roots can exceed the native Unix socket path limit.
    with TemporaryDirectory(prefix="mship-stream-", dir="/tmp") as socket_dir:
        anyio.run(scenario, backend="asyncio")


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


def test_workspace_config_edit_rebuilds_subapp(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = workspace / "mothership.yaml"
    config.write_text("workspace: before\nrepos: {}\n")
    store = _seed(home, [_entry("ws-e", "edited", workspace)])
    built = []

    def build(entry, **kw):
        built.append(entry.path)
        return FakeSubApp(entry.name)

    app = create_host_app(store, auth_token=None, build_subapp=build)
    with TestClient(app) as client:
        client.get("/workspaces/ws-e/specs")
        config.write_text("workspace: after-a-valid-edit\nrepos: {}\n")
        client.get("/workspaces/ws-e/specs")

    assert built == [str(workspace), str(workspace)]


### #471 Task 3 — tiered auth, forwarded-header rewrite, identity + runner ###

LIVE_BEARER = "0123456789abcdef.live-secret"


@pytest.fixture
def bearer_app(tmp_path):
    """Host app in its #471 shape: a short-lived bearer verifier for callers,
    the standing token repurposed as the internal sub-app credential."""
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])
    built: dict[str, FakeSubApp] = {}

    def build(entry, **_kwargs):
        sub = FakeSubApp(entry.name)
        built[entry.id] = sub
        return sub

    app = create_host_app(
        store,
        auth_token="standing",
        verify_bearer=lambda presented: presented == LIVE_BEARER,
        exchange_refresh=lambda refresh: (
            ("0123456789abcdef.minted", 300) if refresh == "good-refresh" else None
        ),
        build_subapp=build,
    )
    return app, built


def test_short_lived_bearer_authorizes_list_and_forwarded_call(bearer_app):
    """The forward must rewrite Authorization: the sub-app knows only the
    standing token, so passing the caller's bearer through 401s every call."""
    app, built = bearer_app
    auth = {"Authorization": f"Bearer {LIVE_BEARER}"}
    with TestClient(app) as client:
        assert client.get("/workspaces", headers=auth).status_code == 200
        forwarded = client.get("/workspaces/ws-a/specs", headers=auth)

    assert forwarded.status_code == 200
    assert built["ws-a"].seen_authorization == "Bearer standing"


def test_foreign_bearer_is_rejected_on_the_list_and_the_forward(bearer_app):
    app, _built = bearer_app
    auth = {"Authorization": "Bearer 0123456789abcdef.minted-elsewhere"}
    with TestClient(app) as client:
        assert client.get("/workspaces", headers=auth).status_code == 401
        assert client.get("/workspaces/ws-a/specs", headers=auth).status_code == 401


def test_real_token_stores_compose_through_the_exchange(tmp_path):
    """The whole loop over the shipped stores, on a stepped clock: refresh in,
    bearer out, bearer authorizes, bearer expires, revoked refresh mints no
    more. Nothing here is stubbed but the wall clock."""
    from mship.core.daemon.host_auth import RefreshStore
    from mship.core.daemon.host_token import issue_host_token, verify_host_token
    from mship.core.relay.token_clock import AnchoredClock

    home = tmp_path / "home"
    now = {"t": 1_000.0}
    clock = AnchoredClock(
        wall=lambda: now["t"], mono=lambda: now["t"], epoch="test-epoch"
    )
    refresh_store = RefreshStore(home, clock=lambda: now["t"])
    refresh = refresh_store.issue_refresh(host_id="hst-1", client="phone")

    def exchange(credential):
        if refresh_store.verify_refresh(credential) is None:
            return None
        return issue_host_token(home, ttl_seconds=300, clock=clock), 300

    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])
    app = create_host_app(
        store,
        auth_token="standing",
        verify_bearer=lambda presented: (
            verify_host_token(home, presented, clock=clock) is not None
        ),
        exchange_refresh=exchange,
        build_subapp=lambda e, **kw: FakeSubApp(e.name),
    )

    with TestClient(app) as client:
        # Flip the last character to one it CANNOT already be: appending a fixed
        # hex digit leaves the credential unchanged 1 run in 16, and that run
        # asserts 401 against the genuine credential.
        tampered = client.post(
            "/host/token",
            json={"refresh": refresh[:-1] + ("1" if refresh[-1] == "0" else "0")},
        )
        minted = client.post("/host/token", json={"refresh": refresh}).json()
        auth = {"Authorization": f"Bearer {minted['token']}"}
        live = client.get("/workspaces", headers=auth)
        now["t"] += minted["expires_in"] + 1
        after_expiry = client.get("/workspaces", headers=auth)
        refresh_store.revoke(host_id="hst-1", client="phone")
        revoked = client.post("/host/token", json={"refresh": refresh})

    assert tampered.status_code == 401
    assert live.status_code == 200
    assert after_expiry.status_code == 401
    assert revoked.status_code == 401


@pytest.mark.parametrize(
    ("edge_header", "value"),
    [
        ("X-Forwarded-For", "203.0.113.7"),
        ("X-Forwarded-Host", "hst-abc.relay.example"),
        ("X-Forwarded-Proto", "https"),
    ],
)
def test_standing_token_works_direct_but_never_over_the_relay(
    bearer_app, edge_header, value
):
    """AC9: no standing credential authorizes relay-borne traffic, while
    first-time LAN/loopback pairing still works. Every header the edge may
    stamp counts — Caddy sets all three, but only one is needed to give the
    request away."""
    app, _built = bearer_app
    standing = {"Authorization": "Bearer standing"}
    with TestClient(app) as client:
        assert client.get("/workspaces", headers=standing).status_code == 200
        relay_borne = client.get(
            "/workspaces", headers={**standing, edge_header: value}
        )

    assert relay_borne.status_code == 401


@pytest.fixture
def relay_domain_app(tmp_path):
    """The same shape as `bearer_app`, but told which domain the relay serves."""
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])
    return create_host_app(
        store,
        auth_token="standing",
        verify_bearer=lambda presented: presented == LIVE_BEARER,
        relay_domain="relay.example",
        build_subapp=lambda entry, **_kwargs: FakeSubApp(entry.name),
    )


@pytest.mark.parametrize(
    ("host_header", "expected"),
    [
        ("hst-abc.relay.example", 401),      # our subdomain, headers stripped
        ("HST-ABC.Relay.Example:443", 401),  # DNS is case-insensitive; so is this
        ("relay.example", 401),              # the relay's own name
        ("notrelay.example", 200),           # merely ends with the same letters
        ("192.168.1.5:47190", 200),          # the LAN pairing path (AC9)
    ],
)
def test_relay_host_header_is_relay_borne_even_with_edge_headers_stripped(
    relay_domain_app, host_header, expected
):
    """Defense in depth for AC9: `_EDGE_HEADERS` is the edge's own testimony, so
    a proxy misconfigured to strip them would silently re-open the standing
    token to the whole internet. The `Host` the request was addressed to is the
    second, independent witness — nothing on the LAN reaches this app under the
    relay's domain."""
    with TestClient(relay_domain_app) as client:
        response = client.get(
            "/workspaces",
            headers={"Authorization": "Bearer standing", "Host": host_header},
        )

    assert response.status_code == expected


def test_every_guarded_route_401s_without_a_credential(bearer_app):
    app, _built = bearer_app
    with TestClient(app) as client:
        assert client.get("/workspaces").status_code == 401
        assert client.post("/workspaces/refresh").status_code == 401
        assert client.get("/workspaces/ws-a/specs").status_code == 401


def test_host_token_exchange_needs_no_bearer(bearer_app):
    """The bootstrap route cannot require the credential it exists to mint."""
    app, _built = bearer_app
    with TestClient(app) as client:
        minted = client.post("/host/token", json={"refresh": "good-refresh"})
        form_minted = client.post(
            "/host/token", data={"refresh": "good-refresh"}
        )
        rejected = client.post("/host/token", json={"refresh": "revoked-refresh"})

    assert minted.status_code == 200
    assert minted.json() == {"token": "0123456789abcdef.minted", "expires_in": 300}
    assert form_minted.status_code == 200
    assert form_minted.json() == minted.json()
    assert rejected.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"refresh": "x" * 4096},   # over the bound
        {"refresh": ""},           # empty
        {"refresh": 17},           # wrong type
        {},                        # missing the field
        [],                        # not an object
    ],
)
def test_host_token_answers_401_for_every_malformed_body(bearer_app, body):
    """A 422 here would make the unauthenticated route an oracle separating
    "malformed" from "wrong" — every failure reads the same."""
    app, _built = bearer_app
    with TestClient(app) as client:
        assert client.post("/host/token", json=body).status_code == 401
        assert client.post("/host/token", content=b"not json").status_code == 401


def test_host_token_stops_reading_as_soon_as_the_body_limit_is_exceeded(
    bearer_app,
):
    """The public exchange must reject the first oversized chunk without
    requesting the unbounded remainder from the ASGI receive channel."""
    import asyncio

    app, _built = bearer_app

    async def drive():
        receive_calls = 0
        sent = []

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls > 1:
                raise AssertionError("oversized request body was still being buffered")
            return {
                "type": "http.request",
                "body": b"x" * 2048,
                "more_body": True,
            }

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/host/token",
            "raw_path": b"/host/token",
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 1),
            "server": ("test", 80),
        }
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), timeout=5)
        return receive_calls, sent

    receive_calls, sent = asyncio.run(drive())
    response_start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    assert receive_calls == 1
    assert response_start["status"] == 401


def test_host_token_route_is_absent_without_an_exchange(tmp_path):
    """No refresh store means nothing to mint: the route does not exist rather
    than 401ing on a credential this host could never honour."""
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])
    app = create_host_app(
        store,
        auth_token="standing",
        verify_bearer=lambda _presented: False,
        build_subapp=lambda e, **kw: FakeSubApp(e.name),
    )

    with TestClient(app) as client:
        assert client.post("/host/token", json={"refresh": "x"}).status_code == 404


def test_health_needs_no_bearer_and_reports_identity(tmp_path):
    """The daemon's own read-back and GC's ladder both poll /health, so it
    stays unauthenticated (and writes nothing — AC11)."""
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])
    app = create_host_app(
        store,
        auth_token="standing",
        verify_bearer=lambda _presented: False,
        host_id="hst-20260817-abcd1234",
        instance_id="0123456789abcdef",
        host_state=lambda: {"state": "online", "subdomain": "hst-abc"},
        build_subapp=lambda e, **kw: FakeSubApp(e.name),
    )

    with TestClient(app) as client:
        health = client.get("/health")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "host_id": "hst-20260817-abcd1234",
        "instance_id": "0123456789abcdef",
        "workspaces": 1,
        "degraded": 0,
        "tunnel": {"state": "online", "subdomain": "hst-abc"},
        "runner": {"enabled": False, "state": "disabled"},
    }


def test_health_reports_disabled_tunnel_when_no_relay_is_configured(tmp_path):
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])
    app = create_host_app(
        store, auth_token=None, build_subapp=lambda e, **kw: FakeSubApp(e.name)
    )

    with TestClient(app) as client:
        health = client.get("/health").json()

    assert health["tunnel"] == {"state": "disabled"}
    assert health["host_id"] is None and health["instance_id"] is None


def test_health_runner_reads_the_injected_host_runner_config(tmp_path):
    """The host-level runner rides the same projection as a workspace's — this
    is the seam #473 fills; unwired, the host reports no runner."""
    home = tmp_path / "home"
    store = _seed(home, [_entry("ws-a", "a", tmp_path / "a")])
    app = create_host_app(
        store,
        auth_token=None,
        runner_config=lambda: {"enabled": True, "max_concurrency": 1},
        build_subapp=lambda e, **kw: FakeSubApp(e.name),
    )

    with TestClient(app) as client:
        assert client.get("/health").json()["runner"] == {
            "enabled": True,
            "state": "unknown",
        }


@pytest.mark.parametrize(
    ("raw_runner", "projected"),
    [
        ({"enabled": True, "max_concurrency": 2}, {"enabled": True, "state": "unknown"}),
        ({"enabled": False}, {"enabled": False, "state": "disabled"}),
        (None, {"enabled": False, "state": "disabled"}),
    ],
)
def test_workspaces_project_runner_and_pass_metarepo_repos_through(
    tmp_path, raw_runner, projected
):
    """AC6: #473's opaque `runner:` block rides this projection (#471 reports
    unknown, never a state it cannot know). Assumption 1: the metarepo shape
    passes through the same projection untouched."""
    from mship.core.daemon.discovery import scan_roots
    from mship.core.daemon.registry import DaemonConfig
    from tests.core.daemon.test_discovery import _mk_metarepo

    home = tmp_path / "home"
    roots = tmp_path / "roots"
    workspace = _mk_metarepo(roots, "meta")
    (candidate,) = scan_roots(DaemonConfig(scan_roots=[str(roots)]))
    entry = _entry("ws-meta", "meta", workspace, repos=candidate.repos, runner=raw_runner)
    store = _seed(home, [entry])
    app = create_host_app(
        store, auth_token=None, build_subapp=lambda e, **kw: FakeSubApp(e.name)
    )

    with TestClient(app) as client:
        (listed,) = client.get("/workspaces").json()["workspaces"]

    assert listed["runner"] == projected
    assert listed["repos"] == [r.model_dump() for r in candidate.repos]


def test_workspaces_runner_projection_degrades_a_malformed_block(tmp_path):
    """A non-dict `runner:` must read as disabled, never 500. Driven off an
    in-memory state because a persisted one cannot reach the projection at all:
    `RegistryStore._load_nolock` answers a failed `model_validate` with an
    EMPTY `RegistryState`, dropping the whole registry rather than one entry."""
    from mship.core.daemon.registry import RegistryState

    entry = _entry("ws-a", "a", tmp_path / "a")
    entry.runner = "not-a-block"  # assignment: the constructor would reject it

    class _MemoryStore:
        def load(self):
            return RegistryState(entries=[entry])

    app = create_host_app(
        _MemoryStore(), auth_token=None, build_subapp=lambda e, **kw: FakeSubApp(e.name)
    )

    with TestClient(app) as client:
        listed = client.get("/workspaces")

    assert listed.status_code == 200
    assert listed.json()["workspaces"][0]["runner"] == {
        "enabled": False,
        "state": "disabled",
    }


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


def test_cold_workspace_startup_does_not_block_routes_when_pr_watch_lane_is_full(
    tmp_path, monkeypatch
):
    """A cold watcher must release cache ownership before waiting for its lane."""
    from threading import Event

    import anyio
    import httpx

    from mship.core import serve as serve_mod
    from mship.core.async_runtime import _limiter_for
    from mship.core.serve import create_app as create_workspace_app
    from mship.core.state import StateManager

    watcher_built = asyncio.Event()
    swept = Event()
    rescanned = Event()

    class Watcher:
        def __init__(self, *_args, **_kwargs):
            watcher_built.set()

        def check_once(self):
            swept.set()

    monkeypatch.setattr(serve_mod, "PrWatcher", Watcher)
    store = _seed(tmp_path / "home", [
        _entry("cold", "cold", tmp_path / "cold"),
        _entry("warm", "warm", tmp_path / "warm"),
    ])

    def build(entry, **_kwargs):
        root = Path(entry.path)
        return create_workspace_app(
            specs_dir=root / "specs", state_manager=StateManager(root / ".mothership"),
            log_manager=None, workspace_root=root, workspace_name=entry.name,
            pr_watch_interval=60 if entry.id == "cold" else 0,
        )

    host = create_host_app(
        store, auth_token=None, build_subapp=build, rescan=rescanned.set
    )

    async def scenario():
        async with host.router.lifespan_context(host):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host), base_url="http://host"
            ) as client:
                with anyio.fail_after(2):
                    assert (await client.get("/workspaces/warm/health")).status_code == 200
                lane = _limiter_for("pr_watch")
                await lane.acquire()
                pending = []
                try:
                    cold = asyncio.create_task(client.get("/workspaces/cold/health"))
                    pending.append(cold)
                    await _wait_for(watcher_built)
                    await anyio.wait_all_tasks_blocked()
                    for method, path in [
                        ("GET", "/workspaces/warm/health"),
                        ("GET", "/health"),
                        ("POST", "/workspaces/refresh"),
                    ]:
                        pending.append(asyncio.create_task(client.request(method, path)))
                    done, blocked = await asyncio.wait(pending, timeout=1)
                    assert not blocked, "cold watcher held the host cache lock during lane I/O"
                    assert len(done) == 4
                    assert all(response.result().status_code == 200 for response in done)
                    assert rescanned.is_set()
                    assert not swept.is_set()
                finally:
                    lane.release()
                    # Let startup finish and publish its cache entry before
                    # host shutdown; cancellation here races that publication.
                    _, unfinished = await asyncio.wait(pending, timeout=2)
                    for request in unfinished:
                        request.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                with anyio.fail_after(1):
                    while not swept.is_set():
                        await anyio.sleep(0)

    asyncio.run(scenario())


def test_workspace_refresh_replacement_and_host_shutdown_drain_watchers(
    tmp_path, monkeypatch
):
    """Every cached serve app owns one watcher and fully drains it before the
    cache replaces/removes the app or the host lifespan itself exits."""
    from contextlib import asynccontextmanager
    from threading import Event, Thread

    from mship.core import serve as serve_mod
    from mship.core.serve import create_app as create_workspace_app
    from mship.core.state import StateManager

    class BlockingSecondSweep:
        instances = []

        def __init__(self, *_args, **_kwargs):
            self.calls = 0
            self.active = False
            self.entered = Event()
            self.release = Event()
            self.created_while_active = sum(
                probe.active for probe in self.__class__.instances
            )
            self.__class__.instances.append(self)

        def check_once(self):
            self.calls += 1
            if self.calls != 2:
                return
            self.active = True
            self.entered.set()
            try:
                self.release.wait()
            finally:
                self.active = False

    monkeypatch.setattr(serve_mod, "PrWatcher", BlockingSecondSweep)

    home = tmp_path / "home"
    before = tmp_path / "before"
    store = _seed(home, [_entry("ws-a", "a", before)])
    subapp_stops = []

    def build(entry, **_kwargs):
        root = Path(entry.path)
        app = create_workspace_app(
            specs_dir=root / "specs",
            state_manager=StateManager(root / ".mothership"),
            log_manager=None,
            workspace_root=root,
            workspace_name=entry.name,
            pr_watch_interval=0.01,
        )
        original_lifespan = app.router.lifespan_context
        stop_reached = Event()
        subapp_stops.append(stop_reached)

        @asynccontextmanager
        async def observed_lifespan(lifespan_app):
            async with original_lifespan(lifespan_app):
                try:
                    yield
                finally:
                    stop_reached.set()

        app.router.lifespan_context = observed_lifespan
        return app

    host = create_host_app(
        store,
        auth_token=None,
        build_subapp=build,
        pr_watch_interval=0.01,
    )
    client = TestClient(host)
    client.__enter__()
    closed = False
    observations = {}

    def in_thread(call):
        done = Event()
        result = []
        error = []

        def run():
            try:
                result.append(call())
            except BaseException as exc:  # surfaced on the test thread below
                error.append(exc)
            finally:
                done.set()

        thread = Thread(target=run)
        thread.start()
        return thread, done, result, error

    def finish_call(thread, done, result, error):
        assert done.wait(2)
        thread.join(timeout=2)
        assert not thread.is_alive()
        if error:
            raise error[0]
        return result[0] if result else None

    try:
        assert client.get("/workspaces/ws-a/health").status_code == 200
        assert client.get("/workspaces/ws-a/health").status_code == 200
        assert len(BlockingSecondSweep.instances) == 1
        first = BlockingSecondSweep.instances[0]
        assert first.entered.wait(1)

        moved = tmp_path / "moved"
        store.mutate(
            lambda state: state.entries.__setitem__(
                0, _entry("ws-a", "a", moved)
            )
        )
        replace = in_thread(lambda: client.get("/workspaces/ws-a/health"))
        assert subapp_stops[0].wait(1)
        observations["replacement_finished_while_old_active"] = replace[1].is_set()
        first.release.set()
        assert finish_call(*replace).status_code == 200
        assert len(BlockingSecondSweep.instances) == 2
        second = BlockingSecondSweep.instances[1]
        observations["replacement_overlap"] = second.created_while_active
        assert second.entered.wait(1)

        added = tmp_path / "added"
        store.mutate(
            lambda state: state.entries.__setitem__(
                0, _entry("ws-b", "b", added)
            )
        )
        refresh = in_thread(lambda: client.post("/workspaces/refresh"))
        assert subapp_stops[1].wait(1)
        observations["refresh_finished_while_removed_active"] = refresh[1].is_set()
        second.release.set()
        assert finish_call(*refresh).status_code == 200
        assert len(BlockingSecondSweep.instances) == 2

        assert client.get("/workspaces/ws-b/health").status_code == 200
        assert client.get("/workspaces/ws-b/health").status_code == 200
        assert len(BlockingSecondSweep.instances) == 3
        third = BlockingSecondSweep.instances[2]
        observations["addition_overlap"] = third.created_while_active
        assert third.entered.wait(1)

        shutdown = in_thread(lambda: client.__exit__(None, None, None))
        assert subapp_stops[2].wait(1)
        observations["shutdown_finished_while_active"] = shutdown[1].is_set()
        third.release.set()
        finish_call(*shutdown)
        closed = True
        observations["active_after_shutdown"] = sum(
            probe.active for probe in BlockingSecondSweep.instances
        )
    finally:
        for probe in BlockingSecondSweep.instances:
            probe.release.set()
        if not closed:
            client.__exit__(None, None, None)

    assert observations == {
        "replacement_finished_while_old_active": False,
        "replacement_overlap": 0,
        "refresh_finished_while_removed_active": False,
        "addition_overlap": 0,
        "shutdown_finished_while_active": False,
        "active_after_shutdown": 0,
    }


@pytest.mark.parametrize("transition", ["replacement", "removal"])
def test_cancelled_subapp_transition_keeps_old_lifespan_until_drain(
    tmp_path, transition
):
    """Cancelling cache replacement/removal cannot let a successor overlap."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import anyio
    import httpx
    from fastapi import FastAPI

    home = tmp_path / "home"
    before = tmp_path / "before"
    moved = tmp_path / "moved"
    store = _seed(home, [_entry("ws-a", "a", before)])
    built = []
    active = 0

    def build(entry, **_kwargs):
        nonlocal active
        stop_reached = anyio.Event()
        allow_drain = anyio.Event()
        drained = anyio.Event()
        block_drain = not built
        probe = SimpleNamespace(
            stop_reached=stop_reached,
            allow_drain=allow_drain,
            drained=drained,
            started=False,
            created_while_active=active,
        )

        @asynccontextmanager
        async def lifespan(_app):
            nonlocal active
            probe.started = True
            active += 1
            try:
                yield
            finally:
                stop_reached.set()
                if block_drain:
                    await allow_drain.wait()
                active -= 1
                drained.set()

        app = FastAPI(lifespan=lifespan)

        @app.get("/health")
        def health():
            return {"status": "ok", "workspace": entry.name}

        built.append(probe)
        return app

    host = create_host_app(store, auth_token=None, build_subapp=build)

    async def scenario():
        first = None
        try:
            async with host.router.lifespan_context(host):
                transport = httpx.ASGITransport(app=host)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    response = await client.get("/workspaces/ws-a/health")
                    assert response.status_code == 200
                    first = built[0]

                    if transition == "replacement":
                        store.mutate(
                            lambda state: state.entries.__setitem__(
                                0, _entry("ws-a", "a", moved)
                            )
                        )

                        async def transition_call():
                            await client.get("/workspaces/ws-a/health")

                    else:
                        store.mutate(lambda state: state.entries.clear())

                        async def transition_call():
                            await host.state.drop_stale_subapps()

                    transition_done = anyio.Event()
                    successor_done = anyio.Event()
                    successor_response = []

                    async def interrupt_transition(
                        *, task_status=anyio.TASK_STATUS_IGNORED
                    ):
                        with anyio.CancelScope() as scope:
                            task_status.started(scope)
                            await transition_call()
                        transition_done.set()

                    async def request_successor():
                        successor_response.append(
                            await client.get("/workspaces/ws-a/health")
                        )
                        successor_done.set()

                    async with anyio.create_task_group() as task_group:
                        scope = await task_group.start(interrupt_transition)
                        await first.stop_reached.wait()
                        scope.cancel()
                        await anyio.wait_all_tasks_blocked()

                        if transition == "removal":
                            store.mutate(
                                lambda state: state.entries.append(
                                    _entry("ws-a", "a", moved)
                                )
                            )

                        task_group.start_soon(request_successor)
                        await anyio.wait_all_tasks_blocked()
                        observed_before_drain = {
                            "transition_done": transition_done.is_set(),
                            "successor_done": successor_done.is_set(),
                            "built": len(built),
                            "active": active,
                        }

                        first.allow_drain.set()
                        await successor_done.wait()

                    assert observed_before_drain == {
                        "transition_done": False,
                        "successor_done": False,
                        "built": 1,
                        "active": 1,
                    }
                    assert first.drained.is_set()
                    started = [probe for probe in built if probe.started]
                    assert len(started) == 2
                    assert started[1].created_while_active == 0
                    assert successor_response[0].status_code == 200
        finally:
            if first is not None:
                first.allow_drain.set()

    anyio.run(scenario, backend="asyncio")
