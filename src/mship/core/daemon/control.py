"""Daemon control app + client-side socket probe.

`version` is `mship.__version__` captured ONCE by the caller at process start —
the guarded single source of truth (`src/mship/__init__.py`, pinned by
`tests/test_version.py`). Not importlib.metadata: both sides answering
"unknown" on absent dist metadata would mask real skew.

Filesystem perms on the unix socket are the auth; no bearer token locally.
Remote traffic stays on the #471 tunnel path.
"""
from __future__ import annotations

import asyncio

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# CLI<->daemon control-protocol version; bump on breaking payload changes.
# 2: capabilities.registry/serve became real + /workspaces endpoints (#472).
PROTOCOL = 2

_PROBE_TIMEOUT_S = 3.0


def create_control_app(
    *,
    started_at: datetime,
    version: str,
    socket_path: str,
    store=None,
    rescan=None,
    after_rescan=None,
    serve_bound: bool = False,
):
    """Tiny closure app factory (the `core/serve.py::create_app` style).

    `store` (a RegistryStore) enables the registry capability + /workspaces
    endpoints over the control socket; `rescan()` re-runs discovery+reconcile.
    `after_rescan()` lets the sibling TCP host app stop stale workspace
    lifespans after a control-socket refresh. `serve_bound` reports whether the
    TCP host app is up (#472).
    """
    from fastapi import FastAPI

    app = FastAPI(title="mshipd control", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health():
        now = datetime.now(timezone.utc)
        return {
            "status": "ok",
            "pid": os.getpid(),
            "mship_version": version,
            "protocol": PROTOCOL,
            "started_at": started_at.isoformat(),
            "uptime_s": (now - started_at).total_seconds(),
            "socket": socket_path,
            "capabilities": {
                "serve": serve_bound,  # #472: TCP host app bound (config-dependent)
                "tunnel": False,  # #471: relay tunnel registration
                "registry": store is not None,  # #472: workspace discovery/registry
                "runner": False,  # #473: unattended worker supervision
            },
        }

    if store is not None:
        @app.get("/workspaces")
        def workspaces():
            return {
                "workspaces": [
                    {"id": e.id, "name": e.name, "path": e.path, "state": e.state, "detail": e.detail}
                    for e in store.load().entries
                    if not e.ignored
                ]
            }

        @app.post("/workspaces/refresh")
        async def refresh():
            if rescan is not None:
                await asyncio.get_running_loop().run_in_executor(None, rescan)
            if after_rescan is not None:
                await after_rescan()
            return {"workspaces": len(store.load().entries)}

    return app


def probe_control_socket(
    socket_path: str | Path, *, client_factory: Callable | None = None
) -> dict | None:
    """GET /health over the unix socket. Never raises (the
    `relay/health.py::probe_health` contract): any failure → None."""
    try:
        if client_factory is None:
            import httpx

            client_factory = lambda **kw: httpx.Client(
                transport=httpx.HTTPTransport(uds=str(socket_path)), **kw
            )
        client = client_factory(timeout=_PROBE_TIMEOUT_S)
        try:
            r = client.get("http://mshipd/health")
            if r.status_code != 200:
                return None
            return r.json()
        finally:
            client.close()
    except Exception:
        return None
