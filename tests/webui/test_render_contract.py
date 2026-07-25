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
    "edges": [{
        "kind": "relay", "name": "relay", "status": "fail",
        "code": "relay_unreachable", "detail": "down",
        "fix": "restart serve", "facts": {"host": "h"},
    }],
}


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

    # Jinja2Templates injects `request` itself; everything else must be payload.
    extra = set(seen["context"]) - set(PAYLOAD) - {"request"}
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


def test_templates_render_no_value_absent_from_the_payload():
    """Every top-level value the templates interpolate exists in the contract."""
    import re
    from pathlib import Path

    from mship.webui import TEMPLATES_DIR

    rendered = set()
    for tpl in Path(TEMPLATES_DIR).glob("*.html"):
        for match in re.finditer(r"\{\{\s*([a-z_]+)\s*\}\}", tpl.read_text()):
            rendered.add(match.group(1))
    # `edge`/`edge.*` are loop locals, not top-level context keys.
    rendered -= {"edge"}
    missing = rendered - set(PAYLOAD)
    assert missing == set(), f"templates render values absent from the payload: {missing}"
    assert "mship_version" in rendered, "ac14: the version must be shown"
