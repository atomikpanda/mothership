"""Workspace-addressed host app (#472): one HTTP surface serving N workspaces.

`create_host_app` exposes host routes (`GET /health`, `GET /workspaces`,
`POST /workspaces/refresh`) plus a catch-all `/workspaces/{id}/{path}` that
resolves the id against the registry and forwards via ASGI to a cached,
lazily built `core/serve.py::create_app` sub-app — one per healthy entry.

NOT `app.mount`: Starlette neither supports mutating a mount table safely on
refresh nor runs mounted sub-apps' lifespan events at all — the PrWatcher in
`create_app`'s lifespan would silently never start. Instead each sub-app's
lifespan is entered explicitly on first build and exited when a refresh
removes/degrades its entry, under a per-host `AsyncExitStack`-style supervisor
guarded by a lock.

Addressing is by ID only — name-in-URL would reintroduce the same-name
ambiguity the id exists to kill. Degraded/missing ids → 503 with the stored
reason (matches #471's "workspace unavailable" ladder); unknown ids → 404.

Auth is tiered (#471): callers present a short-lived host bearer, checked by an
injected `verify_bearer` — the app never holds the material. The standing
`ensure_host_token` string survives in two narrow roles: the credential the
sub-apps verify (rewritten into the forwarded scope, since a sub-app knows
nothing of host bearers), and a direct/loopback-origin fallback so first-time
LAN pairing works before any bearer exists (AC9 — never over the relay). The
guard rides an `APIRouter`, not the app: `POST /host/token` cannot require the
credential it mints, and `/health` is what the daemon and GC poll to decide
reachability, so both stay open.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from mship.core.daemon.capabilities import runner_block
from mship.core.daemon.control import RESCAN_ERROR_STATUS
from mship.core.daemon.registry import RegistryReadError, RegistryStore, WorkspaceEntry
from mship.core.workspace_context import ContextError


def _credential_paths(home: Path) -> tuple[Path, Path, Path]:
    from mship.core.daemon.paths import daemon_state_dir

    state_dir = daemon_state_dir(home)
    return (
        state_dir / "serve-token",
        state_dir / "gh-app-id",
        state_dir / "gh-app-key.pem",
    )


def _atomic_write_owner_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


def persist_host_token(home: Path, token: str) -> None:
    """Atomically persist the token inherited by a supervisor-launched daemon."""
    token_path, _app_id_path, _app_key_path = _credential_paths(home)
    _atomic_write_owner_file(token_path, (token + "\n").encode())


def persist_gh_app_credentials(home: Path, app_id: str, private_key: str) -> None:
    """Persist validated App credentials for the supervisor-launched daemon."""
    _token_path, app_id_path, app_key_path = _credential_paths(home)
    _atomic_write_owner_file(app_id_path, (app_id + "\n").encode())
    _atomic_write_owner_file(app_key_path, private_key.encode())


def load_host_token_override(env: Mapping[str, str]) -> str | None:
    """Return the canonical host-token environment override, when configured."""
    raw = env.get("MSHIP_SERVE_TOKEN")
    if raw is None:
        return None
    token = raw.strip()
    if not token:
        raise ValueError("MSHIP_SERVE_TOKEN must not be blank")
    return token


def load_gh_app_credentials(
    home: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Load App id + private-key text, preferring the invoking environment."""
    env = os.environ if env is None else env
    app_id = env.get("MSHIP_GH_APP_ID") or None
    key_path = env.get("MSHIP_GH_APP_KEY")
    if bool(app_id) != bool(key_path):
        raise ValueError(
            "MSHIP_GH_APP_ID and MSHIP_GH_APP_KEY must be set together"
        )
    if app_id is not None or key_path:
        private_key = None
        if key_path:
            path = Path(key_path)
            if not path.is_file():
                raise ValueError(
                    f"MSHIP_GH_APP_KEY is set but not a readable file ({key_path!r})"
                )
            try:
                private_key = path.read_text()
            except OSError as exc:
                raise ValueError(
                    f"MSHIP_GH_APP_KEY is set but not a readable file ({key_path!r})"
                ) from exc
            if not private_key.strip():
                raise ValueError(
                    f"MSHIP_GH_APP_KEY contains a blank private key ({key_path!r})"
                )
        return app_id, private_key

    if home is None:
        return None, None
    _token_path, app_id_path, app_key_path = _credential_paths(home)
    try:
        persisted_id = app_id_path.read_text().strip()
    except FileNotFoundError:
        if not app_id_path.exists() and not app_key_path.exists():
            return None, None
        raise ValueError(
            "persisted GitHub App credentials are incomplete: expected both "
            f"{app_id_path} and {app_key_path}"
        )
    except OSError as exc:
        raise ValueError(
            f"cannot read persisted GitHub App id {app_id_path}: {exc}"
        ) from exc
    try:
        persisted_key = app_key_path.read_text()
    except FileNotFoundError:
        raise ValueError(
            "persisted GitHub App credentials are incomplete: expected both "
            f"{app_id_path} and {app_key_path}"
        )
    except OSError as exc:
        raise ValueError(
            f"cannot read persisted GitHub App key {app_key_path}: {exc}"
        ) from exc
    if not persisted_id:
        raise ValueError(f"persisted GitHub App id is blank: {app_id_path}")
    if not persisted_key.strip():
        raise ValueError(f"persisted GitHub App key is blank: {app_key_path}")
    return persisted_id, persisted_key


def ensure_host_token(home: Path, *, env: Mapping[str, str]) -> str:
    """Host-level bearer token (env>file>generate shape of relay/token.py's
    ensure_serve_token, but per OS user, not per workspace)."""
    override = load_host_token_override(env)
    if override is not None:
        return override
    path, _app_id_path, _app_key_path = _credential_paths(home)
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError(f"cannot read host token {path}: {exc}") from exc
    token = secrets.token_urlsafe(32)
    persist_host_token(home, token)
    return token


def _fingerprint(entry: WorkspaceEntry) -> tuple:
    """What a cached sub-app was built FROM. Any change here means the cached
    app holds a stale workspace root / state dir / config and must be rebuilt."""
    try:
        stat = Path(entry.config_path).stat()
        config_revision = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        config_revision = None
    return (entry.path, entry.config_path, entry.state, config_revision)


class _SubApp:
    """A built per-workspace serve app plus its running lifespan."""

    def __init__(self, app, fingerprint: tuple) -> None:
        self.app = app
        self.fingerprint = fingerprint
        self._cm = None

    async def start(self) -> None:
        # Enter the sub-app's lifespan explicitly (mounted apps never get it).
        self._cm = self.app.router.lifespan_context(self.app)
        await self._cm.__aenter__()

    async def stop(self) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
            self._cm = None


def _default_build_subapp(
    entry: WorkspaceEntry,
    *,
    auth_token: str | None,
    pr_watch_interval: float | None,
    gh_app_id: str | None,
    gh_app_key: str | None,
):
    from mship.core.serve import create_app
    from mship.core.spec_store import SPECS_DIRNAME
    from mship.core.workspace_context import build_workspace_context

    ctx = build_workspace_context(Path(entry.config_path))
    return create_app(
        specs_dir=ctx.workspace_root / SPECS_DIRNAME,
        state_manager=ctx.state_manager,
        log_manager=ctx.log_manager,
        workspace_root=ctx.workspace_root,
        workspace_name=ctx.config.workspace,
        auth_token=auth_token,
        worktree_manager=ctx.worktree_manager,
        config=ctx.config,
        gh_app_id=gh_app_id,
        gh_app_key=gh_app_key,
        pr_watch_interval=pr_watch_interval,
    )


_BEARER_PREFIX = "Bearer "

# A relay-borne request arrives through Caddy → sish → the ssh -R tunnel, so it
# hits this app from loopback exactly like a direct one; the headers the edge
# stamps are the only honest discriminator. A direct caller who forges one only
# denies itself the standing-token fallback, so the failure direction is safe.
_EDGE_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")


def _is_relay_borne(request: Request) -> bool:
    return any(name in request.headers for name in _EDGE_HEADERS)


def _make_host_auth_dependency(
    token: str | None, verify_bearer: Callable[[str], bool] | None
):
    """Accept a short-lived host bearer; fall back to the standing token for
    direct origins only, plus the namespaced UI's token/cookie exchange."""
    import hmac
    import time

    from fastapi import Header

    from mship.webui import COOKIE_NAME, _cookie_is_valid

    expected = f"Bearer {token}".encode() if token is not None else b""

    def require_host_credential(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        provided = authorization or ""
        if verify_bearer is not None and provided.startswith(_BEARER_PREFIX):
            if verify_bearer(provided[len(_BEARER_PREFIX):]):
                return
        if token is None or _is_relay_borne(request):
            raise HTTPException(
                status_code=401, detail="missing or invalid bearer token"
            )
        if hmac.compare_digest(provided.encode(), expected):
            return
        segments = request.url.path.split("/")
        is_workspace_ui = any(
            segments[index] == "workspaces"
            and segments[index + 1]
            and segments[index + 2] == "ui"
            for index in range(len(segments) - 2)
        )
        if is_workspace_ui:
            supplied = request.query_params.get("token") or ""
            if hmac.compare_digest(supplied.encode(), token.encode()):
                return
            cookie = request.cookies.get(COOKIE_NAME) or ""
            if cookie and _cookie_is_valid(cookie, token, now=int(time.time())):
                return
        raise HTTPException(
            status_code=401, detail="missing or invalid bearer token"
        )

    return require_host_credential


# A refresh credential is `<16 hex>.<64 hex>`; the bound is generous slack over
# that, not a schema. Bounded like the enroll app's `_EnrollBody`, because this
# route is unauthenticated by design.
_MAX_REFRESH_LEN = 512


def _presented_refresh(body: bytes) -> str | None:
    """The refresh credential in a `/host/token` body, or None if there isn't
    one. Every malformed shape collapses to None so the route answers a uniform
    401: a 422 for an over-long or non-JSON body would make the endpoint an
    oracle that separates "malformed" from "wrong"."""
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    credential = payload.get("refresh")
    if not isinstance(credential, str) or not credential:
        return None
    if len(credential) > _MAX_REFRESH_LEN:
        return None
    return credential


def _with_internal_authorization(
    headers: list[tuple[bytes, bytes]], token: str | None
) -> list[tuple[bytes, bytes]]:
    """Swap the caller's credential for the sub-app's own.

    Sub-apps verify the standing token (`core/serve.py`'s single-string
    dependency), so forwarding a short-lived host bearer 401s every call — and
    the caller's credential has no business inside a workspace app anyway.
    """
    rewritten = [(k, v) for k, v in headers if k.lower() != b"authorization"]
    if token is not None:
        rewritten.append((b"authorization", f"Bearer {token}".encode()))
    return rewritten


def create_host_app(
    store: RegistryStore,
    *,
    auth_token: str | None,
    verify_bearer: Callable[[str], bool] | None = None,
    exchange_refresh: Callable[[str], tuple[str, float] | None] | None = None,
    host_id: str | None = None,
    instance_id: str | None = None,
    host_state: Callable[[], Mapping] | None = None,
    runner_config: Callable[[], Any] | None = None,
    build_subapp: Callable = _default_build_subapp,
    rescan: Callable | None = None,
    pr_watch_interval: float | None = None,
    gh_app_id: str | None = None,
    gh_app_key: str | None = None,
):
    """Build the host FastAPI app over a registry store.

    `rescan()` (optional) re-runs discovery+reconcile and returns nothing; the
    refresh route calls it before diffing the registry. `build_subapp` is the
    injectable seam for tests.

    Auth material stays outside: `verify_bearer(presented)` decides whether a
    short-lived host bearer is live, and `exchange_refresh(credential)` returns
    `(token, expires_in)` for a valid refresh credential (None → 401), which is
    what `POST /host/token` publishes. Without an exchange the route is not
    registered at all — a host with no refresh store has nothing to mint.
    `host_id`/`instance_id`/`host_state()` are the identity and tunnel state
    `/health` reports for the daemon's read-back and GC's ladder, and
    `runner_config()` is the host-level runner block `/health` projects — the
    seam #473 fills; absent, the host reports no runner.
    """
    from contextlib import asynccontextmanager

    if pr_watch_interval is None:
        from mship.core.serve import PR_WATCH_INTERVAL_SECONDS

        pr_watch_interval = PR_WATCH_INTERVAL_SECONDS


    subapps: dict[str, _SubApp] = {}
    lock = asyncio.Lock()

    @asynccontextmanager
    async def _lifespan(_app):
        try:
            yield
        finally:
            async with lock:
                for sub in subapps.values():
                    await sub.stop()
                subapps.clear()

    app = FastAPI(title="mship host", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=_lifespan)
    # On a router, NOT the app: `POST /host/token` mints the very credential a
    # blanket app-level dependency would demand of it, and `/health` is the
    # unauthenticated reachability probe the daemon and GC both poll.
    guarded = APIRouter(
        dependencies=(
            [Depends(_make_host_auth_dependency(auth_token, verify_bearer))]
            if (auth_token or verify_bearer)
            else []
        )
    )

    def _entries() -> list[WorkspaceEntry]:
        return store.load().entries

    async def _get_subapp(entry: WorkspaceEntry) -> _SubApp:
        fp = _fingerprint(entry)
        async with lock:
            sub = subapps.get(entry.id)
            if sub is not None and sub.fingerprint != fp:
                # Same id, different path/config (moved workspace, edited yaml):
                # the cached app still points at the OLD root and state dir.
                # Checked per request, so a rescan through ANY path — the host
                # refresh route or the control socket's — takes effect.
                await subapps.pop(entry.id).stop()
                sub = None
            if sub is None:
                sub = _SubApp(
                    build_subapp(
                        entry,
                        auth_token=auth_token,
                        pr_watch_interval=pr_watch_interval,
                        gh_app_id=gh_app_id,
                        gh_app_key=gh_app_key,
                    ),
                    fp,
                )
                await sub.start()
                subapps[entry.id] = sub
            return sub

    async def _drop_stale() -> None:
        healthy_ids = {
            e.id for e in _entries() if e.state == "healthy" and not e.ignored
        }
        async with lock:
            for wid in list(subapps):
                if wid not in healthy_ids:
                    await subapps.pop(wid).stop()

    # The control-socket refresh route runs the same registry rescan but lives
    # in a sibling ASGI app. Expose only the post-rescan cleanup it must await.
    app.state.drop_stale_subapps = _drop_stale

    @app.get("/health")
    def health():
        entries = _entries()
        return {
            "status": "ok",
            "host_id": host_id,
            "instance_id": instance_id,
            "workspaces": len([e for e in entries if not e.ignored]),
            "degraded": len([e for e in entries if e.state == "degraded" and not e.ignored]),
            "tunnel": dict(host_state()) if host_state is not None else {"state": "disabled"},
            "runner": runner_block(
                runner_config() if runner_config is not None else None
            ),
        }

    if exchange_refresh is not None:
        @app.post("/host/token")
        async def mint_host_token(request: Request):
            """The bootstrap exchange (AC9): the phone's persisted refresh
            credential in, a short-lived bearer out — from the host that will
            verify it, never proxied and never published into the directory.

            The body is read here rather than declared as a model so that a
            malformed one fails exactly like a wrong credential (401), instead
            of a 422 that tells an unauthenticated caller which it was.
            """
            credential = _presented_refresh(await request.body())
            granted = exchange_refresh(credential) if credential else None
            if granted is None:
                raise HTTPException(
                    status_code=401, detail="invalid or expired refresh credential"
                )
            token, expires_in = granted
            return {"token": token, "expires_in": expires_in}

    @guarded.get("/workspaces")
    def list_workspaces():
        return {
            "workspaces": [
                {
                    "id": e.id,
                    "name": e.name,
                    "path": e.path,
                    "state": e.state,
                    "detail": e.detail,
                    "repos": [r.model_dump() for r in e.repos],
                    "runtime": e.runtime.model_dump(),
                    "runner": runner_block(e.runner),
                }
                for e in _entries()
                if not e.ignored
            ]
        }

    @guarded.post("/workspaces/refresh")
    async def refresh():
        if rescan is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(None, rescan)
            except (ValueError, RegistryReadError) as exc:
                raise HTTPException(
                    status_code=RESCAN_ERROR_STATUS, detail=str(exc)
                ) from exc
        await _drop_stale()
        return {"workspaces": len(_entries())}

    @guarded.api_route(
        "/workspaces/{workspace_id}/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def forward(workspace_id: str, path: str, request: Request):
        entry = next((e for e in _entries() if e.id == workspace_id and not e.ignored), None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown workspace id {workspace_id!r}")
        if entry.state != "healthy":
            raise HTTPException(
                status_code=503,
                detail=f"workspace {entry.name!r} is {entry.state}: {entry.detail or 'unavailable'}",
            )
        try:
            sub = await _get_subapp(entry)
        except ContextError as e:
            # The registry advertised it, but the workspace won't build now
            # (deleted/edited between scan and request) — 503 with the reason,
            # never an opaque 500.
            raise HTTPException(status_code=503, detail=f"workspace {entry.name!r} unavailable: {e}")

        # ASGI path remains the full request path while root_path identifies the
        # host-owned workspace prefix. Starlette removes root_path for sub-app
        # route matching and appends mounted /ui for URL/cookie generation.
        scope = dict(request.scope)
        host_root = request.scope.get("root_path", "").rstrip("/")
        workspace_root = f"{host_root}/workspaces/{workspace_id}"
        subapp_path = "/" + path
        scope["root_path"] = workspace_root
        scope["path"] = workspace_root + subapp_path
        scope["raw_path"] = scope["path"].encode()
        scope["headers"] = _with_internal_authorization(
            scope.get("headers", []), auth_token
        )

        # STREAMED, not buffered: `POST /exec/{verb}` is consumed with
        # iter_raw() to render task output live, and a client disconnect must
        # reach the serve-side cancellation event. Buffering the whole response
        # would stall output until the task exits and keep runaway tasks alive
        # after the caller hung up.
        start: asyncio.Queue = asyncio.Queue(maxsize=1)
        # BOUNDED for backpressure: an unbounded queue lets the workspace app
        # race ahead of a slow client and buffer a whole `exec` stream in
        # daemon memory — the unbounded-buffer OOM class #469 calls out. With a
        # bound, the sub-app blocks until the client consumes.
        chunks: asyncio.Queue = asyncio.Queue(maxsize=8)

        async def send(message):
            if message["type"] == "http.response.start":
                await start.put(message)
            elif message["type"] == "http.response.body":
                await chunks.put(message.get("body", b""))
                if not message.get("more_body", False):
                    await chunks.put(None)

        async def run_subapp():
            try:
                await sub.app(scope, request.receive, send)
            except Exception:
                if start.empty():
                    await start.put({"type": "http.response.start", "status": 500, "headers": []})
                await chunks.put(None)
                raise
            finally:
                if start.empty():  # sub-app returned without starting a response
                    await start.put({"type": "http.response.start", "status": 500, "headers": []})
                    await chunks.put(None)

        task = asyncio.create_task(run_subapp())
        started = await start.get()

        async def body_stream():
            try:
                while True:
                    chunk = await chunks.get()
                    if chunk is None:
                        break
                    if chunk:
                        yield chunk
            finally:
                if not task.done():
                    task.cancel()  # client hung up → propagate to the serve app

        return StreamingResponse(
            body_stream(),
            status_code=started.get("status", 500),
            headers={k.decode(): v.decode() for k, v in started.get("headers", [])},
        )

    app.include_router(guarded)
    return app
