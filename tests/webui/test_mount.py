import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mship.webui import mount_webui


def _payload():
    return {
        "version": 1, "mship_version": "0.5.20", "workspace": "ws",
        "probed_at": "2026-07-25T16:00:00+00:00",
        "edges": [{
            "kind": "relay", "name": "relay", "status": "ok", "code": "relay_ok",
            "detail": "reachable", "fix": None, "facts": {},
        }],
    }


def test_mount_serves_the_console():
    app = FastAPI()
    mount_webui(app, payload_source=_payload)
    with TestClient(app) as client:
        r = client.get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "relay" in r.text


def test_mount_serves_the_stylesheet():
    app = FastAPI()
    mount_webui(app, payload_source=_payload)
    with TestClient(app) as client:
        r = client.get("/ui/static/app.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


def test_the_package_never_imports_mship_internals():
    """ac1/ac3 structurally: the frontend takes a payload dict and reaches into
    nothing. If this fails, the frontend is no longer separately shippable.

    Parses the AST rather than grepping the text — the modules legitimately
    *mention* `mship.core` in prose explaining why they don't import it.
    """
    import ast
    from pathlib import Path

    import mship.webui as pkg

    package_dir = Path(pkg.__file__).parent
    offenders = []
    for module in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.startswith("mship.") and not name.startswith("mship.webui"):
                    offenders.append(f"{module.name}: {name}")
    assert offenders == [], (
        f"the frontend imports mship internals {offenders} — it must consume "
        f"only the topology payload"
    )
