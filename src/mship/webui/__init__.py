"""The serve-host management console — a self-contained frontend package.

ISOLATION IS THE POINT. Everything the console needs (templates, stylesheet,
Tailwind input, static assets) lives in this directory, and the only thing that
crosses the boundary is the topology payload — the same JSON an external client
gets from `GET /net/topology`. Two rules keep it that way:

1. Nothing in here imports mship internals: no config objects, no stores, no
   `mship.core.*`. The caller supplies `payload_source`; this package never
   learns where the payload came from. (Enforced by a test that greps this
   package for those imports.)
2. `mship.core.serve` couples to this package through `mount_webui` and nothing
   else — no template names, no asset paths, no /ui routes over there.

Consequence: replacing this package with a separately-shipped frontend is a
re-skin, not a rewrite. Build the new client against the versioned payload,
enable CORS for its origin (auth is already a header-borne bearer, not a
same-origin cookie), then delete this directory and its one mount line. The Jinja
templates are deliberately disposable — no effort is spent making them reusable
as a component library.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

# Imported at MODULE level on purpose. With `from __future__ import annotations`
# the route's `request: Request` annotation is a string, and FastAPI resolves it
# against this module's globals — a function-local import would leave it
# unresolved, and FastAPI would treat `request` as a required QUERY PARAM (a
# confusing 422 on every page load).
from fastapi import Request

_PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = _PACKAGE_DIR / "templates"
STATIC_DIR = _PACKAGE_DIR / "static"

MOUNT_PATH = "/ui"


def mount_webui(app, *, payload_source: Callable[[], dict]) -> None:
    """Attach the console to `app` at /ui.

    `payload_source` returns the `GET /net/topology` payload — a plain dict. It
    is the ONLY data this package receives, which is what makes the frontend
    separately shippable (see the module docstring).
    """
    from fastapi import APIRouter
    from fastapi.staticfiles import StaticFiles

    from mship.webui.views import render_topology

    router = APIRouter()

    @router.get(MOUNT_PATH, include_in_schema=False)
    def ui(request: Request):
        return render_topology(request, payload_source())

    app.include_router(router)
    app.mount(
        f"{MOUNT_PATH}/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="webui-static",
    )
