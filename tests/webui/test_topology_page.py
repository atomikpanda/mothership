from fastapi import FastAPI
from fastapi.testclient import TestClient

from mship.webui import mount_webui


def _html(edges):
    app = FastAPI()
    mount_webui(app, payload_source=lambda: {
        "version": 1, "mship_version": "0.5.20", "workspace": "ws",
        "probed_at": "t", "edges": edges,
    })
    with TestClient(app) as client:
        return client.get("/ui").text


def test_unmapped_role_shows_the_prefilled_command():
    html = _html([{
        "kind": "run_host", "name": "run_host:mac-studio", "status": "fail",
        "code": "run_host_unmapped", "detail": "no connection mapped",
        "fix": "run `mship run-host add mac-studio`",
        "facts": {"role": "mac-studio"},
    }])
    # the ROLE is filled in, not a <role> placeholder
    assert "mship run-host add mac-studio" in html
    assert "&lt;role&gt;" not in html


def test_relay_setup_command_is_shown():
    html = _html([{
        "kind": "relay", "name": "relay", "status": "absent",
        "code": "relay_not_configured", "detail": "not running",
        "fix": "run `mship serve --relay`",
        "facts": {"host": "relay.example.com", "relay_configured": True},
    }])
    assert "mship serve --relay" in html


def test_every_command_card_has_a_copy_affordance():
    html = _html([{
        "kind": "run_host", "name": "run_host:mac", "status": "fail",
        "code": "run_host_unmapped", "detail": "x", "fix": "y",
        "facts": {"role": "mac"},
    }])
    assert "data-copy" in html


def test_healthy_topology_shows_no_commands():
    html = _html([{
        "kind": "relay", "name": "relay", "status": "ok", "code": "relay_ok",
        "detail": "reachable", "fix": None, "facts": {},
    }])
    assert "data-copy" not in html


def test_an_unknown_code_yields_no_card_rather_than_raising():
    """The frontend does not import the topology module, so a code it has never
    heard of must degrade to 'no card', not an error."""
    html = _html([{
        "kind": "future", "name": "future", "status": "fail",
        "code": "code_from_a_newer_mship", "detail": "x", "fix": "y", "facts": {},
    }])
    assert "data-copy" not in html
    assert "future" in html


def test_no_mutating_routes_exist():
    """ac9: the console performs no privileged mutation — it is GET-only."""
    app = FastAPI()
    mount_webui(app, payload_source=lambda: {
        "version": 1, "mship_version": "x", "workspace": "ws",
        "probed_at": "t", "edges": [],
    })
    methods = set()
    for route in app.routes:
        methods |= getattr(route, "methods", set()) or set()
    assert methods <= {"GET", "HEAD"}, f"console exposes {methods - {'GET', 'HEAD'}}"
