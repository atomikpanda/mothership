from __future__ import annotations

import html
import os
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
from mship.cli.view._placeholders import placeholder_for
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
    resolve_task,
)
from mship.core.view.spec_discovery import SpecNotFoundError, find_spec
from mship.core.view.web_port import NoFreePortError, pick_port


class SpecView(ViewApp):
    def __init__(
        self,
        workspace_root: Path,
        name_or_path: Optional[str],
        *,
        task: Optional[str] = None,
        state_manager=None,
        state=None,
        log_manager=None,
        cli_task: Optional[str] = None,
        cwd: Optional[Path] = None,
        **kw,
    ):
        # Strip SpecView-specific kwargs before passing to super
        for k in ("workspace_root", "name_or_path", "task",
                  "state_manager", "state", "log_manager",
                  "cli_task", "cwd"):
            kw.pop(k, None)
        super().__init__(**kw)
        self._workspace_root = workspace_root
        self._name_or_path = name_or_path
        self._task_filter = task
        self._state_manager = state_manager
        self._initial_state = state  # kept for existing tests that pre-load state
        self._log_manager = log_manager
        self._cli_task = cli_task
        self._cwd = cwd if cwd is not None else Path.cwd()
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

    def _current_state(self):
        """Return fresh state from state_manager if available; else the
        pre-loaded state passed in at construction (kept for unit tests)."""
        if self._state_manager is not None:
            return self._state_manager.load()
        return self._initial_state

    def _resolve_task_slug(self, state) -> Optional[str]:
        """Return the task slug to render for this tick, or raise a resolver
        error. Returns None when `name_or_path` is set (resolution skipped).

        Only called when state_manager is set (watch-mode with new API).
        """
        if self._name_or_path is not None:
            return None
        if self._task_filter is not None:
            return self._task_filter
        task = resolve_task(
            state,
            cli_task=self._cli_task,
            env_task=os.environ.get("MSHIP_TASK"),
            cwd=self._cwd,
        )
        return task.slug

    def _refresh_content(self) -> None:
        assert self._markdown is not None
        assert self._error_static is not None
        assert self._body is not None
        was_at_end = self._body.scroll_y >= (self._body.max_scroll_y - 1)
        prev_y = self._body.scroll_y

        state = self._current_state()

        # Per-tick resolver: only active when state_manager is set (watch
        # mode wired with the new API). Legacy callers that pass state= and
        # task= directly bypass this path.
        if self._state_manager is not None:
            try:
                slug_for_tick = self._resolve_task_slug(state)
            except (NoActiveTaskError, AmbiguousTaskError, UnknownTaskError) as err:
                text = placeholder_for(err)
                self._last_source = text
                self._last_error = ""
                self._markdown.update(text)
                self._error_static.update("")
                self.call_after_refresh(self._restore_scroll, prev_y, was_at_end)
                return
        else:
            slug_for_tick = self._task_filter

        try:
            path = find_spec(
                self._workspace_root,
                self._name_or_path,
                task=slug_for_tick,
                state=state,
            )
            source = path.read_text()
            self._last_source = source
            self._last_error = ""
            self._markdown.update(source)
            self._error_static.update("")
        except SpecNotFoundError as e:
            if self._name_or_path is None:
                body = self._render_task_fallback(slug_for_tick, state, default_error=str(e))
                self._last_source = body
                self._last_error = ""
                self._markdown.update(body)
                self._error_static.update("")
            else:
                error_msg = f"Spec not found: {e}"
                self._last_source = ""
                self._last_error = error_msg
                self._markdown.update("")
                self._error_static.update(error_msg)
        self.call_after_refresh(self._restore_scroll, prev_y, was_at_end)

    def _render_task_fallback(self, slug: Optional[str], state, *, default_error: str) -> str:
        """Build a markdown document for the 'no spec yet' case.

        Uses the `slug` + `state` passed in. Returns just the error text when
        no slug is set or the slug isn't in state (safety net for out-of-band
        callers).
        """
        if slug is None or state is None or slug not in state.tasks:
            return f"# {default_error}\n"

        task = state.tasks[slug]
        phase = task.phase
        branch = task.branch
        description = task.description or "_(no description)_"

        lines: list[str] = [
            f"# No spec yet for task `{slug}`",
            "",
            f"**Phase:** `{phase}`  ·  **Branch:** `{branch}`",
            "",
            "## Task description",
            description,
            "",
            "## Recent journal",
        ]

        entries = []
        if self._log_manager is not None:
            try:
                entries = self._log_manager.read(slug, last=10)
            except TypeError:
                entries = self._log_manager.read(slug)[-10:]

        if not entries:
            lines.append("_No journal entries yet._")
        else:
            for e in entries:
                ts = e.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"- **{ts}** — {e.message}")

        lines.append("")
        lines.append("_Write a spec with your preferred flow and save it to `docs/superpowers/specs/`._")
        return "\n".join(lines) + "\n"

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

        # Resolve target task. If the user specified an explicit spec name,
        # skip task resolution entirely (rendering is name-driven). Otherwise
        # require an anchor via resolve_or_exit.
        resolved_task_slug: Optional[str] = None
        if name_or_path is None:
            from mship.cli._resolve import resolve_or_exit
            t = resolve_or_exit(state, task)
            resolved_task_slug = t.slug

        # Direct render: either name_or_path or a resolved task slug.
        if web:
            try:
                path = find_spec(
                    workspace_root, name_or_path, task=resolved_task_slug, state=state,
                )
            except SpecNotFoundError as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(code=1)
            _serve_web(path, port)
            return
        view = SpecView(
            workspace_root=workspace_root,
            name_or_path=name_or_path,
            task=resolved_task_slug,
            state=state,
            log_manager=container.log_manager(),
            watch=watch,
            interval=interval,
        )
        view.run()
