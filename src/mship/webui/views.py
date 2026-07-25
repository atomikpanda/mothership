"""The one view. Payload in, HTML out.

The render context is EXACTLY the topology payload, edge-for-edge — see
`tests/webui/test_render_contract.py`. No Python objects, config, or store
handles enter a template, so an external frontend could produce the same page
from the endpoint response alone.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from mship.webui import TEMPLATES_DIR
from mship.webui.actions import command_for

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
    context["edges"] = [
        {**edge, "action": command_for(edge)} for edge in payload.get("edges", [])
    ]
    return _templates.TemplateResponse(request, "topology.html", context)
