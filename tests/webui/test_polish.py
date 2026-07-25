"""The four console polish items: severity order, staleness/auto-refresh,
workspace health, and the pairing QR."""
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mship.webui import REFRESH_SECONDS, mount_webui

SHELL = {"mship_version": "0.5.25", "probed_at": "2026-07-25T23:00:00+00:00"}


def _edge(name, status, code="c", **kw):
    base = {"kind": "k", "name": name, "status": status, "code": code,
            "detail": "d", "fix": "f", "facts": {}}
    base.update(kw)
    return base


def _topology(edges):
    return {"version": 1, "workspace": "ws", "edges": edges, **SHELL}


def _client(*, edges=None, doctor=None, pair=None):
    app = FastAPI()
    mount_webui(
        app,
        payload_source=lambda: _topology(edges if edges is not None else []),
        doctor_source=(lambda: doctor) if doctor else None,
        pair_source=(lambda: pair) if pair else None,
    )
    return TestClient(app, client=("127.0.0.1", 4242))


# --- 1. severity order ------------------------------------------------------

def test_edges_render_worst_first():
    edges = [
        _edge("ok-one", "ok"), _edge("absent-one", "absent"),
        _edge("fail-one", "fail"), _edge("warn-one", "warn"),
    ]
    with _client(edges=edges) as c:
        html = c.get("/ui/").text
    order = [m for m in re.findall(r"(fail-one|warn-one|absent-one|ok-one)", html)]
    firsts = []
    for name in order:
        if name not in firsts:
            firsts.append(name)
    assert firsts == ["fail-one", "warn-one", "absent-one", "ok-one"]


def test_equal_severities_keep_the_payload_order():
    """Stable sort: two failures stay in the order the payload listed them, so the
    page does not reshuffle on every probe."""
    edges = [_edge("b-fail", "fail"), _edge("a-fail", "fail")]
    with _client(edges=edges) as c:
        html = c.get("/ui/").text
    assert html.index("b-fail") < html.index("a-fail")


def test_the_endpoint_payload_order_is_untouched():
    """Ordering is a display choice; /net/topology's order is a contract."""
    from mship.core.topology import Edge, Topology, topology_payload

    payload = topology_payload(Topology(
        version=1, workspace="w", probed_at="t",
        edges=[Edge("k", "ok-first", "ok", "c", "d", None),
               Edge("k", "fail-second", "fail", "c", "d", "f")],
    ))
    assert [e["name"] for e in payload["edges"]] == ["ok-first", "fail-second"]


# --- 2. auto-refresh + staleness -------------------------------------------

def test_the_page_refreshes_itself_and_shows_its_age():
    with _client(edges=[_edge("x", "ok")]) as c:
        html = c.get("/ui/").text
    assert f'http-equiv="refresh" content="{REFRESH_SECONDS}"' in html
    assert 'id="age"' in html and 'data-probed-at="2026-07-25T23:00:00+00:00"' in html
    assert "/ui/static/age.js" in html


def test_the_refresh_is_a_reload_not_a_fetch():
    """/net/topology is header-only and the cookie is scoped to /ui, so a JS fetch
    would 401 — the mechanism has to be a plain reload."""
    from pathlib import Path

    from mship.webui import STATIC_DIR

    age_js = (Path(STATIC_DIR) / "age.js").read_text()
    for forbidden in ("fetch(", "XMLHttpRequest", "/net/topology"):
        assert forbidden not in age_js


# --- 3. workspace health ---------------------------------------------------

def test_the_health_page_renders_doctor_checks():
    doctor = {
        "workspace": "ws", "failures": 1, "warnings": 1, **SHELL,
        "checks": [
            {"name": "repo/path", "status": "pass", "message": "path exists"},
            {"name": "gh", "status": "warn", "message": "not authenticated"},
            {"name": "repo/taskfile", "status": "fail", "message": "no go-task file"},
        ],
    }
    with _client(doctor=doctor) as c:
        html = c.get("/ui/doctor").text
    for expected in ("repo/path", "not authenticated", "no go-task file", "3 checks total"):
        assert expected in html


def test_health_is_its_own_page_reachable_from_the_topology_page():
    with _client(edges=[_edge("x", "ok")]) as c:
        assert '/ui/doctor' in c.get("/ui/").text
        assert c.get("/ui/doctor").status_code == 200


# --- 4. pairing QR ---------------------------------------------------------

def test_the_pair_page_renders_a_local_data_uri_and_warns():
    pair = {"workspace": "ws", "qr_data_uri": "data:image/png;base64,iVBORw0KAAA=",
            "unavailable_reason": "", **SHELL}
    with _client(pair=pair) as c:
        html = c.get("/ui/pair").text
    assert "data:image/png;base64," in html
    assert "credential" in html.lower()          # the warning is present
    assert "http://" not in html.replace('href="/ui', '')   # nothing off-host


def test_the_pair_page_explains_itself_when_not_pairable():
    pair = {"workspace": "ws", "qr_data_uri": None, **SHELL,
            "unavailable_reason": "Not pairable from this serve: needs a token."}
    with _client(pair=pair) as c:
        html = c.get("/ui/pair").text
    assert "Not pairable from this serve" in html
    assert "data:image" not in html


def test_the_pairing_page_does_not_auto_refresh():
    """It displays a credential; reloading that on a timer is not a default."""
    pair = {"workspace": "ws", "qr_data_uri": "data:image/png;base64,x",
            "unavailable_reason": "", **SHELL}
    with _client(pair=pair) as c:
        html = c.get("/ui/pair").text
    assert 'http-equiv="refresh"' not in html


def test_the_topology_page_still_carries_no_credential():
    """The QR moved to its own page precisely so this stays true."""
    with _client(edges=[_edge("x", "ok")]) as c:
        html = c.get("/ui/").text
    assert "data:image" not in html
