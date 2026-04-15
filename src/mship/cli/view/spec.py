from __future__ import annotations

import html
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import typer
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Markdown, Static

from mship.cli.view._base import ViewApp
from mship.core.view.spec_discovery import SpecNotFoundError, find_spec
from mship.core.view.web_port import NoFreePortError, pick_port


class SpecView(ViewApp):
    def __init__(
        self,
        workspace_root: Path,
        name_or_path: Optional[str],
        *,
        task: Optional[str] = None,
        state=None,
        **kw,
    ):
        # Strip SpecView-specific kwargs before passing to super
        kw.pop("workspace_root", None)
        kw.pop("name_or_path", None)
        kw.pop("task", None)
        kw.pop("state", None)
        super().__init__(**kw)
        self._workspace_root = workspace_root
        self._name_or_path = name_or_path
        self._task_filter = task
        self._state = state
        self._markdown: Markdown | None = None
        self._error_static: Static | None = None
        self._body: VerticalScroll | None = None
        self._last_source: str = ""
        self._last_error: str = ""

    def compose(self) -> ComposeResult:
        self._markdown = Markdown("")
        self._error_static = Static("", expand=True)
        self._body = VerticalScroll(self._markdown, self._error_static)
        yield self._body

    def gather(self) -> str:  # not used; refresh is overridden directly
        return ""

    def _refresh_content(self) -> None:
        assert self._markdown is not None
        assert self._error_static is not None
        assert self._body is not None
        was_at_end = self._body.scroll_y >= (self._body.max_scroll_y - 1)
        prev_y = self._body.scroll_y
        try:
            path = find_spec(self._workspace_root, self._name_or_path, task=self._task_filter, state=self._state)
            source = path.read_text()
            self._last_source = source
            self._last_error = ""
            self._markdown.update(source)
            self._error_static.update("")
        except SpecNotFoundError as e:
            error_msg = f"Spec not found: {e}"
            self._last_source = ""
            self._last_error = error_msg
            self._markdown.update("")
            self._error_static.update(error_msg)
        self.call_after_refresh(self._restore_scroll, prev_y, was_at_end)

    def rendered_text(self) -> str:
        """Test helper — returns last markdown source plus any error text."""
        return self._last_source + "\n" + self._last_error


def _render_html(spec_path: Path) -> bytes:
    try:
        from markdown_it import MarkdownIt  # type: ignore
        body_html = MarkdownIt().render(spec_path.read_text())
    except ImportError:
        body_html = f"<pre>{html.escape(spec_path.read_text())}</pre>"
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(spec_path.name)}</title>
<style>body{{font-family:system-ui;max-width:780px;margin:2rem auto;padding:0 1rem;line-height:1.55}}
pre,code{{background:#f4f4f4;padding:.1em .3em;border-radius:3px}}
pre{{padding:.6em;overflow:auto}}</style>
</head><body>{body_html}</body></html>"""
    return doc.encode("utf-8")


def serve_spec_web(
    spec_path: Path,
    start_port: int = None,
    explicit_port: int | None = None,
) -> tuple[HTTPServer, int, threading.Thread]:
    port = pick_port(
        start=start_port if start_port is not None else 47213,
        explicit=explicit_port,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = _render_html(spec_path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a, **kw):
            return

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


def _serve_web(path: Path, port: int | None) -> None:
    try:
        server, chosen, _t = serve_spec_web(path, explicit_port=port)
    except NoFreePortError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)
    url = f"http://127.0.0.1:{chosen}/"
    typer.echo(f"Serving {path.name} at {url} (Ctrl-C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        import time as _t2
        while True:
            _t2.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


def register(app: typer.Typer, get_container):
    @app.command()
    def spec(
        name_or_path: Optional[str] = typer.Argument(None),
        watch: bool = typer.Option(False, "--watch"),
        interval: float = typer.Option(2.0, "--interval"),
        web: bool = typer.Option(False, "--web", help="Serve rendered HTML on localhost"),
        port: Optional[int] = typer.Option(None, "--port", help="Explicit port for --web"),
        task: Optional[str] = typer.Option(None, "--task", help="Narrow to one task's worktrees"),
    ):
        """Render a spec file (newest by default)."""
        from pathlib import Path as _P
        if task is not None and name_or_path is not None:
            typer.echo("Error: --task and an explicit spec name are mutually exclusive.", err=True)
            raise typer.Exit(code=1)

        container = get_container()
        workspace_root = _P(container.config_path()).parent
        state = container.state_manager().load()

        if task is not None and task not in state.tasks:
            known = ", ".join(sorted(state.tasks.keys())) or "(none)"
            typer.echo(f"Unknown task '{task}'. Known: {known}.", err=True)
            raise typer.Exit(code=1)

        # Direct render when caller specified a target.
        if name_or_path is not None or task is not None:
            if web:
                try:
                    path = find_spec(workspace_root, name_or_path, task=task, state=state)
                except SpecNotFoundError as e:
                    typer.echo(f"Error: {e}", err=True)
                    raise typer.Exit(code=1)
                _serve_web(path, port)
                return
            view = SpecView(
                workspace_root=workspace_root,
                name_or_path=name_or_path,
                task=task,
                state=state,
                watch=watch,
                interval=interval,
            )
            view.run()
            return

        # No target: open the cross-task spec index picker.
        from mship.cli.view._spec_index import SpecIndexApp
        app_ = SpecIndexApp(
            workspace_root=workspace_root,
            state=state,
            state_loader=container.state_manager().load,
            watch=watch,
            interval=interval,
        )
        app_.run()
