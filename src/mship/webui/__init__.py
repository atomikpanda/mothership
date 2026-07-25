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


def _mint_cookie(auth_token: str, *, now: int, lifetime: int = COOKIE_MAX_AGE_SECONDS) -> str:
    """`<expires-at>.<hmac>` — a credential that the SERVER can expire.

    Never the serve token itself, so a stolen cookie cannot be replayed against
    the JSON API (which wants the raw bearer). The expiry is inside the signed
    material rather than only in the Set-Cookie `max_age`: `max_age` is a request
    to the browser, and a client that ignores it (or a copied cookie value) would
    otherwise stay valid forever.
    """
    import hashlib
    import hmac

    expires_at = now + lifetime
    mac = hmac.new(
        auth_token.encode(), f"mship-ui:{expires_at}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{expires_at}.{mac}"


def _cookie_is_valid(cookie: str, auth_token: str, *, now: int) -> bool:
    """Verify a minted cookie: well-formed, signature matches, not expired."""
    import hashlib
    import hmac

    expires_str, _, mac = cookie.partition(".")
    if not mac:
        return False
    try:
        expires_at = int(expires_str)
    except ValueError:
        return False
    if expires_at <= now:
        return False
    expected = hmac.new(
        auth_token.encode(), f"mship-ui:{expires_at}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(mac, expected)


def _connection_is_secure(request: "Request") -> bool:
    """Whether this request reached us over TLS.

    Behind the relay, Caddy terminates TLS and the request arrives at the app as
    plain HTTP, so the ASGI scheme alone under-reports it — hence the forwarded
    header. Trusting that header is safe in this direction: the only thing it can
    do is make a cookie MORE restrictive (a forged `https` yields a Secure cookie
    the browser then declines to send over http — a self-inflicted annoyance, not
    a privilege gain).
    """
    if request.url.scheme == "https":
        return True
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return forwarded.lower() == "https"


def _is_loopback(request: "Request") -> bool:
    """Whether the request came from this machine, judged by the client address
    rather than the Host header (which a client controls)."""
    client = request.client
    return bool(client) and client.host in {"127.0.0.1", "::1", "localhost"}


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
    import time

    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    from mship.webui.views import render_topology

    def _credentialed(request: Request) -> bool:
        if auth_token is None:
            return True
        header = request.headers.get("authorization") or ""
        if hmac.compare_digest(header.encode(), f"Bearer {auth_token}".encode()):
            return True
        cookie = request.cookies.get(COOKIE_NAME) or ""
        return bool(cookie) and _cookie_is_valid(
            cookie, auth_token, now=int(time.time())
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

            secure = _connection_is_secure(request)
            if not secure and not _is_loopback(request):
                # Refuse to hand a browser credential to a plaintext connection
                # from off-box. The header bearer still works, so this is a
                # narrowing rather than a dead end.
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "refusing to set a console cookie over plaintext from a "
                        "non-loopback client; reach the console over https (the "
                        "relay terminates TLS), tunnel it to localhost, or send "
                        "Authorization: Bearer instead"
                    ),
                )

            response = RedirectResponse(url=str(request.url.path), status_code=303)
            response.set_cookie(
                COOKIE_NAME,
                _mint_cookie(auth_token, now=int(time.time())),
                max_age=COOKIE_MAX_AGE_SECONDS,
                httponly=True,
                samesite="strict",
                path=MOUNT_PATH,
                secure=secure,
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
