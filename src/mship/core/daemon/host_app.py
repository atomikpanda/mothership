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
One host-level token gates everything (the #471 short-lived-token seam).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from mship.core.daemon.registry import RegistryStore, WorkspaceEntry
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


def ensure_host_token(home: Path) -> str:
    """Host-level bearer token (env>file>generate shape of relay/token.py's
    ensure_serve_token, but per OS user, not per workspace)."""
    env = os.environ.get("MSHIP_SERVE_TOKEN")
    if env:
        return env
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


def create_host_app(
    store: RegistryStore,
    *,
    auth_token: str | None,
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
    """
    from contextlib import asynccontextmanager

    from mship.core.serve import _make_auth_dependency

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

    dependencies = [Depends(_make_auth_dependency(auth_token))] if auth_token else []
    app = FastAPI(title="mship host", docs_url=None, redoc_url=None,
                  openapi_url=None, dependencies=dependencies, lifespan=_lifespan)

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
            "workspaces": len([e for e in entries if not e.ignored]),
            "degraded": len([e for e in entries if e.state != "healthy" and not e.ignored]),
        }

    @app.get("/workspaces")
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
                }
                for e in _entries()
                if not e.ignored
            ]
        }

    @app.post("/workspaces/refresh")
    async def refresh():
        if rescan is not None:
            await asyncio.get_running_loop().run_in_executor(None, rescan)
        await _drop_stale()
        return {"workspaces": len(_entries())}

    @app.api_route(
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

        # ASGI forward with the prefix stripped, so create_app's routes see /specs etc.
        scope = dict(request.scope)
        scope["path"] = "/" + path
        scope["raw_path"] = ("/" + path).encode()
        scope["root_path"] = ""

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

    return app
