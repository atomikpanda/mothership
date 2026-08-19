"""Workspace-addressed host app (#472 Task 7)."""
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
