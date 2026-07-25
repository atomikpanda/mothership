"""ac10/ac13: nothing is injectable, and nothing secret is rendered."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mship.webui import mount_webui

SECRET = "SENTINEL-should-never-render"


def _html(payload):
    app = FastAPI()
    mount_webui(app, payload_source=lambda: payload)
    with TestClient(app) as client:
        return client.get("/ui").text


def test_config_derived_values_are_escaped():
    html = _html({
        "version": 1, "mship_version": "0.5.20",
        "workspace": '<script>alert("ws")</script>',
        "probed_at": "t",
        "edges": [{
            "kind": "run_host", "name": "run_host:<img src=x onerror=alert(1)>",
            "status": "fail", "code": "run_host_unmapped",
            "detail": '<script>alert("detail")</script>',
            "fix": "<b>not bold</b>",
            "facts": {"role": "<svg onload=alert(1)>"},
        }],
    })
    # What matters is that no LIVE tag is formed — an inert `onerror=alert(1)`
    # sitting inside escaped angle brackets is just text on the page.
    # The page's own local <script src=...> tags are expected; assert on those
    # explicitly rather than string-stripping one filename, so adding a script
    # cannot quietly weaken this check.
    import re

    scripts = re.findall(r"<script\b[^>]*>", html, flags=re.IGNORECASE)
    for tag in scripts:
        assert re.match(r'<script src="/ui/static/[a-z0-9_.-]+\.js">$', tag), (
            f"unexpected script tag (inline script, or an off-host src): {tag}"
        )
    assert "<img" not in html
    assert "<svg" not in html
    assert "<b>not bold</b>" not in html
    # …and that the hostile input is escaped rather than silently dropped, so the
    # operator can still see the malformed role/hostname they need to fix.
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_a_hostile_role_cannot_break_out_of_the_copy_attribute():
    """The command string lands in a data-copy ATTRIBUTE, so quote handling
    matters as much as tag handling."""
    html = _html({
        "version": 1, "mship_version": "x", "workspace": "ws", "probed_at": "t",
        "edges": [{
            "kind": "run_host", "name": "run_host:evil", "status": "fail",
            "code": "run_host_unmapped", "detail": "x", "fix": "y",
            "facts": {"role": '" onmouseover="alert(1)'},
        }],
    })
    assert 'onmouseover="alert(1)"' not in html
    assert "&#34;" in html or "&quot;" in html


def test_no_safe_filter_on_any_template_value():
    """A single `|safe` would silently undo the escaping above."""
    from pathlib import Path

    from mship.webui import TEMPLATES_DIR

    import re

    for tpl in Path(TEMPLATES_DIR).glob("*.html"):
        # Strip Jinja comments first: a comment that merely MENTIONS the filter
        # (e.g. explaining why it is not used) is not a bypass, and grepping raw
        # text made this guard fire on its own documentation.
        text = re.sub(r"\{#.*?#\}", "", tpl.read_text(), flags=re.S)
        assert "|safe" not in text and "| safe" not in text, f"{tpl.name} uses |safe"
        assert "autoescape false" not in text


def test_facts_are_never_dumped_wholesale():
    """The topology layer already redacts; this is belt-and-braces. The page
    renders named fields only, never the whole facts mapping."""
    html = _html({
        "version": 1, "mship_version": "0.5.20", "workspace": "ws",
        "probed_at": "t",
        "edges": [{
            "kind": "run_host", "name": "run_host:mac", "status": "ok",
            "code": "run_host_ok", "detail": "reachable", "fix": None,
            "facts": {"role": "mac", "token": SECRET},
        }],
    })
    assert SECRET not in html
