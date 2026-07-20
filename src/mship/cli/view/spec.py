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
        header_provider=None,
        **kw,
    ):
        # Strip SpecView-specific kwargs before passing to super
        for k in ("workspace_root", "name_or_path", "task",
                  "state_manager", "state", "log_manager",
                  "cli_task", "cwd", "header_provider"):
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
        self._header_provider = header_provider
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
        task, _ = resolve_task(
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

        # Per-tick resolver: only active in watch mode, where a task may be
        # spawned/changed while the view is open and the "waiting for a task"
        # placeholder is the right thing to show. In non-watch mode the command
        # already resolved the task once (with the MOS-175 fall-back-to-newest
        # when none is active), so trust `self._task_filter` and never override
        # it with the placeholder. Legacy callers that pass state=/task= directly
        # (state_manager is None) also bypass this path.
        if self._state_manager is not None and self._watch:
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
            header = self._header_provider() if self._header_provider else None
            if header:
                source = f"**{header}**\n\n{source}"
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
        workitem: Optional[str] = typer.Option(None, "--workitem", help="Select the spec linked to this WorkItem id"),
        status: Optional[str] = typer.Option(None, "--status", help="Select the newest spec with this status"),
    ):
        """Render a spec file (newest by default)."""
        from pathlib import Path as _P
        if task is not None and name_or_path is not None:
            typer.echo("Error: --task and an explicit spec name are mutually exclusive.", err=True)
            raise typer.Exit(code=1)

        if web and watch:
            typer.echo("Error: --web and --watch are mutually exclusive.", err=True)
            raise typer.Exit(code=1)

        container = get_container()
        workspace_root = _P(container.config_path()).parent
        state = container.state_manager().load()

        from mship.core.spec_store import SPECS_DIRNAME
        from mship.core.workitem_store import WorkItemStore
        from mship.core.view.spec_selection import (
            SpecSelectionError, SpecSelector, load_canonical_specs,
            scan_canonical_specs, select_spec,
        )
        from mship.core.view.headers import header_for_spec
        from mship.cli.view._workitems import load_workitem_index

        active_selectors = [n for n, v in (("--workitem", workitem), ("--status", status)) if v is not None]
        if len(active_selectors) > 1:
            typer.echo("Error: --workitem and --status are mutually exclusive.", err=True)
            raise typer.Exit(code=1)
        if active_selectors and name_or_path is not None:
            typer.echo(f"Error: {active_selectors[0]} and an explicit spec name are mutually exclusive.", err=True)
            raise typer.Exit(code=1)
        if active_selectors and task is not None:
            typer.echo(f"Error: {active_selectors[0]} and --task are mutually exclusive.", err=True)
            raise typer.Exit(code=1)

        specs_dir = workspace_root / SPECS_DIRNAME
        canonical_path: Optional[_P] = None
        canonical_spec_id: Optional[str] = None
        selector: Optional[SpecSelector] = None
        if active_selectors:
            selector = SpecSelector(work_item_id=workitem, status=status)
        elif name_or_path is None and task is None and load_canonical_specs(specs_dir):
            # AC1/AC2: deterministic canonical default (newest by created_at), not
            # worktree/mtime. Only engages when the canonical store has real specs;
            # otherwise fall through to the legacy task/worktree resolution below.
            selector = SpecSelector()
        if selector is not None:
            items = WorkItemStore(_P(container.state_dir()) / "workitems")
            scanned = scan_canonical_specs(specs_dir)
            try:
                spec = select_spec([s for s, _ in scanned], items.list(), selector)
            except SpecSelectionError as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(code=1)
            # Use the REAL path the spec was read from, not a reconstruction, so a
            # file whose name diverges from <date>-<id>.md still renders.
            canonical_path = next((p for s, p in scanned if s.id == spec.id), None)
            canonical_spec_id = spec.id

        # Resolve target task. If the user specified an explicit spec name,
        # skip task resolution entirely (rendering is name-driven). If --watch
        # is set, defer task resolution into the view so resolver errors
        # become placeholder text instead of exit-1.
        resolved_task_slug: Optional[str] = None
        cli_task_for_view: Optional[str] = None
        if canonical_path is None and name_or_path is None:
            if watch:
                cli_task_for_view = task
            elif task is not None:
                # Explicit --task: resolve or exit on unknown/ambiguous.
                from mship.cli._resolve import resolve_or_exit
                t = resolve_or_exit(state, task)
                resolved_task_slug = t.slug
            else:
                # No explicit --task: try to resolve the active task, but fall
                # back to newest spec in workspace if none is active (MOS-175).
                try:
                    t, _ = resolve_task(
                        state, cli_task=None,
                        env_task=os.environ.get("MSHIP_TASK"),
                        cwd=_P.cwd(),
                    )
                    resolved_task_slug = t.slug
                except (NoActiveTaskError, AmbiguousTaskError, UnknownTaskError):
                    resolved_task_slug = None

        # --web still requires a resolvable spec path at request time.
        if web:
            try:
                path = canonical_path or find_spec(
                    workspace_root, name_or_path, task=resolved_task_slug, state=state,
                )
            except SpecNotFoundError as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(code=1)
            _serve_web(path, port)
            return

        # Non-TTY short-circuit (#124): the SpecView TUI hangs forever when
        # stdout isn't a terminal (agent pipes, redirects, CI). Mirror the
        # `mship status` pattern — print the resolved spec to stdout and exit.
        from mship.cli.output import Output
        if not Output().is_tty:
            try:
                path = canonical_path or find_spec(
                    workspace_root, name_or_path, task=resolved_task_slug, state=state,
                )
            except SpecNotFoundError as e:
                typer.echo(f"Error: {e}", err=True)
                raise typer.Exit(code=1)
            typer.echo(path.read_text(), nl=False)
            return

        header_provider = None
        if canonical_spec_id is not None:
            header_provider = lambda: header_for_spec(canonical_spec_id, load_workitem_index(container))
        view = SpecView(
            workspace_root=workspace_root,
            name_or_path=str(canonical_path) if canonical_path is not None else name_or_path,
            task=resolved_task_slug,
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            cli_task=cli_task_for_view,
            cwd=_P.cwd(),
            header_provider=header_provider,
            watch=watch,
            interval=interval,
        )
        view.run()
