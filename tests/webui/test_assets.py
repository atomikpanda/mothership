"""ac11/ac12: assets are self-contained and the page works in both schemes."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mship.webui import STATIC_DIR, mount_webui


def _html():
    app = FastAPI()
    mount_webui(app, payload_source=lambda: {
        "version": 1, "mship_version": "0.5.20", "workspace": "ws",
        "probed_at": "t", "edges": [],
    })
    with TestClient(app) as client:
        return client.get("/ui").text


def test_no_off_host_asset_references():
    """The console must work with no internet: no CDN, font, script, or
    stylesheet fetched from another origin."""
    html = _html()
    for marker in ("http://", "https://", "//cdn", "fonts.googleapis", "unpkg", "jsdelivr"):
        assert marker not in html, f"off-host reference {marker!r} in the page"


def test_asset_references_are_all_local_paths():
    import re

    html = _html()
    for attr in re.finditer(r'(?:href|src)="([^"]+)"', html):
        url = attr.group(1)
        assert url.startswith("/"), f"non-root-relative asset reference: {url}"


def test_stylesheet_is_committed_and_non_trivial():
    css = (Path(STATIC_DIR) / "app.css").read_text()
    assert len(css) > 1000, "app.css looks like a placeholder — run `task webui:css`"


def test_dark_scheme_is_styled():
    css = (Path(STATIC_DIR) / "app.css").read_text()
    assert "prefers-color-scheme" in css, "no dark-scheme rules were generated"


def test_representative_classes_from_the_templates_are_compiled():
    """A spot check that the stylesheet was generated FROM these templates.

    Deliberately not an exhaustive class-by-class sweep: Tailwind escapes
    selectors (`py-0.5` -> `py-0\\.5`), so string-matching every class produces
    false failures. `task webui:css-check` regenerates and diffs, which is the
    authoritative drift guard; this only catches "the stylesheet is from some
    other project entirely".
    """
    css = (Path(STATIC_DIR) / "app.css").read_text()
    for cls in ("rounded-lg", "font-medium", "max-w-4xl", "space-y-3"):
        assert cls in css, f"{cls} used by the templates but absent from app.css"


def test_the_drift_check_target_still_exists():
    """The stylesheet is a committed build artifact; `webui:css-check` is what
    keeps it honest, so silently dropping that target must fail a test."""
    taskfile = Path(__file__).resolve().parents[2] / "Taskfile.yml"
    text = taskfile.read_text()
    assert "webui:css-check" in text
    assert "webui:css:" in text
