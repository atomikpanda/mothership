import threading
import urllib.request
from pathlib import Path

import pytest

from mship.cli.view.spec import SpecView, serve_spec_web


@pytest.mark.asyncio
async def test_spec_view_renders_markdown(tmp_path: Path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "s.md").write_text("# Hello\n\nBody text.\n")
    view = SpecView(workspace_root=tmp_path, name_or_path=None, watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        # SpecView uses Markdown widget; body text should appear
        assert "Body text" in view.rendered_text()


@pytest.mark.asyncio
async def test_spec_view_missing_spec(tmp_path: Path):
    view = SpecView(workspace_root=tmp_path, name_or_path="nope", watch=False, interval=1.0)
    async with view.run_test() as pilot:
        await pilot.pause()
        assert "Spec not found" in view.rendered_text()


def test_serve_spec_web_serves_rendered_html(tmp_path: Path):
    spec = tmp_path / "s.md"
    spec.write_text("# Title\n\nBody.\n")
    server, port, thread = serve_spec_web(spec, start_port=47500)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            html = r.read().decode("utf-8")
        assert "<h1>" in html.lower() or "title" in html.lower()
        assert "body" in html.lower()
    finally:
        server.shutdown()
        thread.join(timeout=2)
