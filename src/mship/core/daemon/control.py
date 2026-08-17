"""Daemon control app + client-side socket probe.

`version` is `mship.__version__` captured ONCE by the caller at process start —
the guarded single source of truth (`src/mship/__init__.py`, pinned by
`tests/test_version.py`). Not importlib.metadata: both sides answering
"unknown" on absent dist metadata would mask real skew.

Filesystem perms on the unix socket are the auth; no bearer token locally.
Remote traffic stays on the #471 tunnel path.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# CLI<->daemon control-protocol version; bump on breaking payload changes.
PROTOCOL = 1

_PROBE_TIMEOUT_S = 3.0


def create_control_app(*, started_at: datetime, version: str, socket_path: str):
    """Tiny closure app factory (the `core/serve.py::create_app` style)."""
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
                "serve": False,  # #472: serve becomes a daemon capability with the registry
                "tunnel": False,  # #471: relay tunnel registration
                "registry": False,  # #472: workspace discovery/registry
                "runner": False,  # #473: unattended worker supervision
            },
        }

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
