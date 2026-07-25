"""Browser-usable auth for the console.

The console shipped header-bearer-only, which made it unreachable in the very
configuration it exists for: with a relay, `MSHIP_SERVE_TOKEN` is set, so every
route demands an `Authorization` header that a browser address bar cannot send —
`/ui` returned 401 in a browser and 200 only to curl.

The fix is Jupyter's: a one-time `?token=` in the URL is exchanged for a
short-lived cookie, and the JSON contract keeps accepting the header so a
separately-shipped frontend still authenticates unchanged.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mship.webui import mount_webui

TOKEN = "s3cret-serve-token"


def _payload():
    return {
        "version": 1, "mship_version": "0.5.23", "workspace": "ws",
        "probed_at": "2026-07-25T19:00:00+00:00",
        "edges": [{
            "kind": "relay", "name": "relay", "status": "ok", "code": "relay_ok",
            "detail": "reachable", "fix": None, "facts": {},
        }],
    }


def _app(*, token: str | None = TOKEN):
    app = FastAPI()
    mount_webui(app, payload_source=_payload, auth_token=token)
    return app


# --- the browser path -------------------------------------------------------

def test_a_browser_with_no_credentials_is_refused():
    with TestClient(_app()) as client:
        r = client.get("/ui/", follow_redirects=False)
    assert r.status_code == 401


def test_token_in_the_url_sets_a_cookie_and_redirects_to_a_clean_url():
    with TestClient(_app()) as client:
        r = client.get(f"/ui/?token={TOKEN}", follow_redirects=False)
        assert r.status_code in (302, 303, 307)
        # the token must not survive in the redirect target
        assert "token" not in r.headers["location"]
        assert r.headers["location"].rstrip("/").endswith("/ui")

        cookie = r.headers.get("set-cookie", "")
        assert cookie, "no cookie was set"
        assert "HttpOnly" in cookie, "cookie must not be readable from JS"
        assert "SameSite=Strict" in cookie or "samesite=strict" in cookie.lower()
        assert "Path=/ui" in cookie or "path=/ui" in cookie.lower(), (
            "cookie should be scoped to the console, not the whole API"
        )
        # the raw serve token must never be the cookie's value
        assert TOKEN not in cookie


def test_the_cookie_then_serves_the_page():
    with TestClient(_app()) as client:
        client.get(f"/ui/?token={TOKEN}", follow_redirects=False)   # sets cookie
        r = client.get("/ui/")
    assert r.status_code == 200
    assert "Connectivity topology" in r.text


def test_a_wrong_token_in_the_url_is_refused_and_sets_nothing():
    with TestClient(_app()) as client:
        r = client.get("/ui/?token=wrong", follow_redirects=False)
    assert r.status_code == 401
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_a_forged_cookie_is_refused():
    with TestClient(_app()) as client:
        client.cookies.set("mship_ui", "made-up-value", path="/ui")
        r = client.get("/ui/", follow_redirects=False)
    assert r.status_code == 401


# --- the API path is untouched (separability) --------------------------------

def test_the_header_bearer_still_works_without_any_cookie():
    with TestClient(_app()) as client:
        r = client.get("/ui/", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_static_assets_require_credentials_too():
    """They were reachable unauthenticated over the relay: StaticFiles mounted
    with app.mount() bypasses an app-level dependency."""
    with TestClient(_app()) as client:
        assert client.get("/ui/static/app.css").status_code == 401
        ok = client.get(
            "/ui/static/app.css", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert ok.status_code == 200
    assert "text/css" in ok.headers["content-type"]


def test_the_page_and_its_stylesheet_both_load_with_only_the_cookie():
    """What a real browser does: one tokenised visit, then it fetches the CSS
    with just the cookie."""
    with TestClient(_app()) as client:
        client.get(f"/ui/?token={TOKEN}", follow_redirects=False)
        assert client.get("/ui/").status_code == 200
        assert client.get("/ui/static/app.css").status_code == 200


# --- the no-auth case (loopback serve with no token) ------------------------

def test_no_token_configured_means_no_auth():
    """A tokenless loopback serve already exposes everything locally; the console
    must not invent an auth requirement that mship itself does not have."""
    with TestClient(_app(token=None)) as client:
        assert client.get("/ui/").status_code == 200
        assert client.get("/ui/static/app.css").status_code == 200


def test_the_url_an_operator_types_reaches_the_console():
    """`/ui` without a trailing slash is what a human types; the mount 307s it to
    `/ui/`, which browsers follow transparently."""
    with TestClient(_app()) as client:
        r = client.get(f"/ui?token={TOKEN}", follow_redirects=True)
    assert r.status_code == 200
    assert "Connectivity topology" in r.text
    # and the credential is gone from where the browser ends up
    assert "token" not in str(r.url)
