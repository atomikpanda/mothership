"""The one view. Payload in, HTML out.

The render context is EXACTLY the topology payload, edge-for-edge — see
`tests/webui/test_render_contract.py`. No Python objects, config, or store
handles enter a template, so an external frontend could produce the same page
from the endpoint response alone.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from mship.webui import REFRESH_SECONDS, TEMPLATES_DIR
from mship.webui.actions import command_for

#: Render order: what is broken first. Deliberately NOT applied to the
#: `/net/topology` payload — that order is part of a published contract, and this
#: is only how one page chooses to display it.
_SEVERITY = {"fail": 0, "warn": 1, "absent": 2, "ok": 3}

#: Autoescaping is on by default here (`select_autoescape`) and must stay on:
#: this page renders config-derived strings (hostnames, subdomains, role names).
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def render_topology(request: Request, payload: dict):
    """Render the topology page from `payload` and nothing else.

    The only transform is `command_for`, a pure function OF THE PAYLOAD — it adds
    no new data source, so an external frontend can derive the same thing from
    the same response. It is attached inside each edge rather than as a new
    top-level key, which keeps the context == payload contract intact.
    """
    context = dict(payload)
    context["refresh_seconds"] = REFRESH_SECONDS
    context["edges"] = sorted(
        ({**edge, "action": command_for(edge)} for edge in payload.get("edges", [])),
        # Stable within a severity so equal edges keep the payload's own order.
        key=lambda e: _SEVERITY.get(e.get("status"), 99),
    )
    return _templates.TemplateResponse(request, "topology.html", context)


def render_doctor(request: Request, payload: dict):
    """Render the workspace-health page from the `/doctor` payload.

    A SEPARATE page rather than a section of the topology page, so each page still
    renders from exactly one endpoint payload — that is what keeps the
    context-equals-payload contract (and therefore separability) intact.
    """
    return _templates.TemplateResponse(
        request, "doctor.html", {**payload, "refresh_seconds": REFRESH_SECONDS}
    )


def render_pair(request: Request, payload: dict):
    """Render the pairing page.

    Its own page for a security reason, not layout: a pairing link EMBEDS the
    serve token, so a QR of it is a rendered credential. Keeping it here leaves the
    topology page genuinely secret-free, which is what the no-secrets test on that
    page is asserting.
    """
    # No auto-refresh here: reloading a page that displays a credential, on a
    # timer, is not a behaviour to add by default.
    return _templates.TemplateResponse(
        request, "pair.html", {**payload, "refresh_seconds": 0}
    )
