"""ac3: the template render context IS the topology payload.

If this fails because a new key was added to the context, the fix is to add it to
the ENDPOINT payload instead — otherwise a separately-shipped frontend cannot
render the page from the endpoint alone, which is the whole point of the
isolation constraint.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mship.webui import mount_webui

PAYLOAD = {
    "version": 1, "mship_version": "0.5.20", "workspace": "ws",
    "probed_at": "2026-07-25T16:00:00+00:00",
    "ui_root": "/ui",
    "edges": [{
        "kind": "relay", "name": "relay", "status": "fail",
        "code": "relay_unreachable", "detail": "down",
        "fix": "restart serve", "facts": {"host": "h"},
    }],
}


#: Console-local DISPLAY settings, allowed in the context but deliberately NOT in
#: any endpoint payload. The contract's purpose is that an external frontend can
#: reproduce a page from one endpoint response — and a refresh interval is a
#: preference such a frontend would choose for itself, not server state it needs
#: to be told. Anything added here must carry no server state, or the contract
#: stops meaning anything.
_CONSOLE_LOCAL = {"refresh_seconds"}

def test_context_contains_nothing_beyond_the_payload(monkeypatch):
    seen = {}

    import mship.webui.views as views

    real = views._templates.TemplateResponse

    def spy(request, name, context, *a, **kw):
        seen["context"] = dict(context)
        return real(request, name, context, *a, **kw)

    monkeypatch.setattr(views._templates, "TemplateResponse", spy)

    app = FastAPI()
    mount_webui(app, payload_source=lambda: PAYLOAD)
    with TestClient(app) as client:
        assert client.get("/ui").status_code == 200

    # Jinja2Templates injects `request` itself, and `_CONSOLE_LOCAL` names are
    # display preferences that deliberately are not endpoint data (see below).
    extra = set(seen["context"]) - set(PAYLOAD) - {"request"} - _CONSOLE_LOCAL
    assert extra == set(), f"context keys not in the payload: {extra}"


def test_per_edge_derivation_stays_inside_edges(monkeypatch):
    """The command card is derived FROM the payload, so it rides inside each
    edge rather than becoming a new top-level context key."""
    seen = {}

    import mship.webui.views as views

    real = views._templates.TemplateResponse

    def spy(request, name, context, *a, **kw):
        seen["context"] = dict(context)
        return real(request, name, context, *a, **kw)

    monkeypatch.setattr(views._templates, "TemplateResponse", spy)

    app = FastAPI()
    mount_webui(app, payload_source=lambda: PAYLOAD)
    with TestClient(app) as client:
        client.get("/ui")

    assert "action" in seen["context"]["edges"][0]
    assert "action" not in seen["context"]


#: Each page renders from exactly ONE endpoint payload. That is the rule that
#: keeps the console separable — a page whose values come from two sources cannot
#: be reproduced by an external client holding one response. The shapes below are
#: the contract each page is allowed to draw on.
_PAGE_PAYLOADS = {
    "topology.html": set(PAYLOAD),
    "doctor.html": {"workspace", "checks", "failures", "warnings",
                    "mship_version", "probed_at", "ui_root"},
    "pair.html": {"workspace", "qr_data_uri", "unavailable_reason",
                  "mship_version", "probed_at", "ui_root"},
}

#: Shared shell: every payload must carry these, plus `request`, which
#: Jinja2Templates injects itself.
_SHELL_KEYS = {"mship_version", "probed_at"}


def _interpolated_names(text: str) -> set[str]:
    import re

    names = set()
    for match in re.finditer(r"\{\{\s*([a-z_]+)", text):
        names.add(match.group(1))
    for match in re.finditer(r"\{%\s*(?:if|for [a-z_]+ in)\s+([a-z_]+)", text):
        names.add(match.group(1))
    return names


def test_every_page_renders_only_values_its_own_payload_carries():
    """The per-page version of the context contract.

    If this fails, the fix is to add the value to that page's ENDPOINT payload —
    not to pass it into the template context, which would make the page
    unreproducible from the endpoint alone.
    """
    from pathlib import Path as _P

    from mship.webui import TEMPLATES_DIR

    shell = _interpolated_names((_P(TEMPLATES_DIR) / "base.html").read_text())
    loop_locals = {"edge", "check", "request"}

    for page, payload_keys in _PAGE_PAYLOADS.items():
        text = (_P(TEMPLATES_DIR) / page).read_text()
        names = (_interpolated_names(text) | shell) - loop_locals - _CONSOLE_LOCAL
        missing = names - payload_keys
        assert missing == set(), (
            f"{page} renders values absent from its payload: {sorted(missing)}"
        )


def test_every_page_payload_carries_the_shared_shell_values():
    """base.html is shared, so every page's payload must satisfy it — including
    the version and probed-at the footer shows."""
    for page, payload_keys in _PAGE_PAYLOADS.items():
        assert _SHELL_KEYS <= payload_keys, (
            f"{page}'s payload is missing shell values: {sorted(_SHELL_KEYS - payload_keys)}"
        )


def test_console_local_settings_carry_no_server_state():
    """The allowlist above is a loophole, so keep it honest: each entry must be a
    display preference, not data. A name that looks like state (a url, a token, a
    path, a count) does not belong there."""
    for name in _CONSOLE_LOCAL:
        assert not any(
            marker in name
            for marker in ("url", "token", "path", "host", "count", "status", "edge")
        ), f"{name!r} looks like server state, not a display preference"
