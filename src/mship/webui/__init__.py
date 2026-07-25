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
enable CORS for its origin, then delete this directory and its one mount line. The
Jinja templates are deliberately disposable — no effort is spent making them
reusable as a component library.

AUTH. The console is mounted as a SUB-APPLICATION rather than added with
`include_router`, because a mounted sub-app does not inherit the parent's
app-level dependencies (verified: `include_router` does, `mount` does not). That
matters twice over:

  * It lets the console accept a browser, which cannot send an `Authorization`
    header from an address bar. Shipped header-only, `/ui` returned 401 in a
    browser whenever a token was configured — i.e. always, with a relay.
  * It closes a hole the previous shape opened: the `StaticFiles` mount already
    bypassed the app-level bearer, so the stylesheet was fetchable
    unauthenticated over the public relay subdomain.

So this package owns its own check: `Authorization: Bearer <token>` (unchanged,
for API clients and a future external frontend) OR a short-lived cookie obtained
by visiting `/ui?token=<token>` once. `GET /net/topology` is NOT part of this
sub-app and stays header-only.
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


#: Cookie the console sets after a `?token=` visit. Scoped to the console path so
#: it is never sent to the JSON API, and HttpOnly so page scripts cannot read it.
COOKIE_NAME = "mship_ui"

#: How long a tokenised visit stays good for. Long enough to use the console,
#: short enough that a shared screenshot of the URL is not a lasting credential.
COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60


def _cookie_value(auth_token: str) -> str:
    """An opaque, deterministic derivation of the serve token — never the token
    itself, so a stolen cookie cannot be replayed against the JSON API (which
    wants the raw bearer). Deterministic so a restart does not invalidate an open
    tab, and compared with `compare_digest`."""
    import hashlib

    return hashlib.sha256(f"mship-ui-cookie:{auth_token}".encode()).hexdigest()


def mount_webui(app, *, payload_source: Callable[[], dict], auth_token: str | None = None) -> None:
    """Attach the console to `app` at /ui.

    `payload_source` returns the `GET /net/topology` payload — a plain dict. It
    is the ONLY data this package receives, which is what makes the frontend
    separately shippable (see the module docstring).

    `auth_token` is the serve bearer, or None when the serve runs without auth
    (a tokenless loopback serve). When None the console adds no auth of its own —
    inventing a requirement mship itself does not have would just lock the
    operator out of a server that is already open locally.
    """
    import hmac

    from fastapi import Depends, FastAPI, HTTPException, Query
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    from mship.webui.views import render_topology

    expected_cookie = _cookie_value(auth_token) if auth_token else None

    def _credentialed(request: Request) -> bool:
        if auth_token is None:
            return True
        header = request.headers.get("authorization") or ""
        if hmac.compare_digest(header.encode(), f"Bearer {auth_token}".encode()):
            return True
        cookie = request.cookies.get(COOKIE_NAME) or ""
        return bool(expected_cookie) and hmac.compare_digest(
            cookie.encode(), expected_cookie.encode()
        )

    # A sub-app, NOT include_router: see the module docstring. This is what lets
    # the console authenticate a browser and what puts its static files behind a
    # check instead of leaving them open over the relay.
    console = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @console.get("/", include_in_schema=False)
    def ui(request: Request, token: str | None = Query(default=None)):
        # Jupyter's exchange: a token in the URL becomes a cookie, then the URL is
        # cleaned so the credential does not linger in history, titles, or a
        # screenshot of the address bar.
        if token is not None and auth_token is not None:
            if not hmac.compare_digest(token.encode(), auth_token.encode()):
                raise HTTPException(status_code=401, detail="invalid token")
            response = RedirectResponse(url=str(request.url.path), status_code=303)
            response.set_cookie(
                COOKIE_NAME,
                expected_cookie,
                max_age=COOKIE_MAX_AGE_SECONDS,
                httponly=True,
                samesite="strict",
                path=MOUNT_PATH,
                secure=request.url.scheme == "https",
            )
            return response

        return render_topology(request, payload_source())

    console.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="webui-static",
    )

    # ONE choke point for the whole sub-app — the page AND its static files.
    # A route dependency could not cover StaticFiles (it is a bare ASGI app), and
    # two separate checks would be two things to keep in agreement.
    @console.middleware("http")
    async def _require_credentials(request: Request, call_next):
        if _credentialed(request):
            return await call_next(request)
        # Let a valid one-time `?token=` through so the route can exchange it for
        # a cookie; anything else is refused here.
        supplied = request.query_params.get("token")
        if (
            supplied is not None
            and auth_token is not None
            and hmac.compare_digest(supplied.encode(), auth_token.encode())
        ):
            return await call_next(request)
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {
                "detail": (
                    "the console needs credentials: open it once as "
                    "/ui?token=<serve token> (or run `mship ui`), or send "
                    "Authorization: Bearer <serve token>"
                )
            },
            status_code=401,
        )

    app.mount(MOUNT_PATH, console, name="webui")
    return None
