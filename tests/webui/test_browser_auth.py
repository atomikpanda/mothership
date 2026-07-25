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


def _client(app, *, host: str = "127.0.0.1"):
    """A client with a real address. TestClient defaults to the host
    "testclient", which is neither loopback nor TLS — and the console refuses to
    hand a cookie to a plaintext non-loopback caller, so the default would look
    like a failure when it is the guard working."""
    return TestClient(app, client=(host, 45678))


# --- the browser path -------------------------------------------------------

def test_a_browser_with_no_credentials_is_refused():
    with _client(_app()) as client:
        r = client.get("/ui/", follow_redirects=False)
    assert r.status_code == 401


def test_token_in_the_url_sets_a_cookie_and_redirects_to_a_clean_url():
    with _client(_app()) as client:
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
    with _client(_app()) as client:
        client.get(f"/ui/?token={TOKEN}", follow_redirects=False)   # sets cookie
        r = client.get("/ui/")
    assert r.status_code == 200
    assert "Connectivity topology" in r.text


def test_a_wrong_token_in_the_url_is_refused_and_sets_nothing():
    with _client(_app()) as client:
        r = client.get("/ui/?token=wrong", follow_redirects=False)
    assert r.status_code == 401
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_a_forged_cookie_is_refused():
    with _client(_app()) as client:
        client.cookies.set("mship_ui", "made-up-value", path="/ui")
        r = client.get("/ui/", follow_redirects=False)
    assert r.status_code == 401


# --- the API path is untouched (separability) --------------------------------

def test_the_header_bearer_still_works_without_any_cookie():
    with _client(_app()) as client:
        r = client.get("/ui/", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_static_assets_require_credentials_too():
    """They were reachable unauthenticated over the relay: StaticFiles mounted
    with app.mount() bypasses an app-level dependency."""
    with _client(_app()) as client:
        assert client.get("/ui/static/app.css").status_code == 401
        ok = client.get(
            "/ui/static/app.css", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert ok.status_code == 200
    assert "text/css" in ok.headers["content-type"]


def test_the_page_and_its_stylesheet_both_load_with_only_the_cookie():
    """What a real browser does: one tokenised visit, then it fetches the CSS
    with just the cookie."""
    with _client(_app()) as client:
        client.get(f"/ui/?token={TOKEN}", follow_redirects=False)
        assert client.get("/ui/").status_code == 200
        assert client.get("/ui/static/app.css").status_code == 200


# --- the no-auth case (loopback serve with no token) ------------------------

def test_no_token_configured_means_no_auth():
    """A tokenless loopback serve already exposes everything locally; the console
    must not invent an auth requirement that mship itself does not have."""
    with _client(_app(token=None)) as client:
        assert client.get("/ui/").status_code == 200
        assert client.get("/ui/static/app.css").status_code == 200


def test_the_url_an_operator_types_reaches_the_console():
    """`/ui` without a trailing slash is what a human types; the mount 307s it to
    `/ui/`, which browsers follow transparently."""
    with _client(_app()) as client:
        r = client.get(f"/ui?token={TOKEN}", follow_redirects=True)
    assert r.status_code == 200
    assert "Connectivity topology" in r.text
    # and the credential is gone from where the browser ends up
    assert "token" not in str(r.url)


# --- where the cookie may be issued at all ----------------------------------

def test_a_plaintext_request_from_off_box_is_refused_a_cookie():
    """The relay terminates TLS, so a plaintext request from a non-loopback
    client means the credential would cross the network in the clear. The header
    bearer still works, so this narrows rather than blocks."""
    with _client(_app(), host="203.0.113.7") as client:
        r = client.get(f"/ui/?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 400
    assert "set-cookie" not in {k.lower() for k in r.headers}
    assert "plaintext" in r.json()["detail"]


def test_a_forwarded_https_request_gets_a_secure_cookie():
    """Behind the relay the app sees plain HTTP, so TLS has to be inferred from
    the forwarded header or every relay cookie would omit Secure."""
    with _client(_app(), host="203.0.113.7") as client:
        r = client.get(
            f"/ui/?token={TOKEN}",
            headers={"x-forwarded-proto": "https"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "Secure" in cookie


def test_loopback_over_plain_http_still_gets_a_cookie_without_secure():
    """A local browser on http://127.0.0.1 must keep working — a Secure cookie
    there would simply never be sent back."""
    with _client(_app()) as client:
        r = client.get(f"/ui/?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 303
    assert "Secure" not in r.headers["set-cookie"]


# --- server-side expiry ------------------------------------------------------

def test_the_cookie_is_rejected_once_it_expires():
    """`max_age` only asks the browser to forget it; the signed expiry is what
    makes the SERVER stop honouring a copied value."""
    from mship.webui import _mint_cookie

    with _client(_app()) as client:
        stale = _mint_cookie(TOKEN, now=0, lifetime=1)     # expired in 1970
        client.cookies.set("mship_ui", stale, path="/ui")
        assert client.get("/ui/", follow_redirects=False).status_code == 401


def test_a_cookie_with_a_tampered_expiry_is_rejected():
    """Pushing the expiry out must break the signature."""
    import time

    from mship.webui import _mint_cookie

    good = _mint_cookie(TOKEN, now=int(time.time()))
    _, _, mac = good.partition(".")
    forged = f"{int(time.time()) + 10_000_000}.{mac}"
    with _client(_app()) as client:
        client.cookies.set("mship_ui", forged, path="/ui")
        assert client.get("/ui/", follow_redirects=False).status_code == 401


def test_a_cookie_minted_from_a_different_token_is_rejected():
    from mship.webui import _mint_cookie
    import time

    with _client(_app()) as client:
        client.cookies.set(
            "mship_ui", _mint_cookie("someone-elses-token", now=int(time.time())),
            path="/ui",
        )
        assert client.get("/ui/", follow_redirects=False).status_code == 401


def test_a_fresh_cookie_is_accepted():
    import time

    from mship.webui import _mint_cookie

    with _client(_app()) as client:
        client.cookies.set("mship_ui", _mint_cookie(TOKEN, now=int(time.time())), path="/ui")
        assert client.get("/ui/").status_code == 200
