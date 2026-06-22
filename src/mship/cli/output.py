"""TTY-aware output with explicit overrides (MOS-103).

Output shape, color, and verbosity are resolved with a documented precedence:

    explicit ctor arg  >  CLI flag (via configure_output)  >  env var  >  TTY

* ``--json`` / ``MSHIP_JSON`` force JSON regardless of TTY state (implies no color).
* ``--quiet`` / ``MSHIP_QUIET`` suppress advisory warnings and breadcrumbs on
  stderr (errors and exit codes are unchanged).
* ``--no-color`` / ``NO_COLOR`` strip ANSI color (per https://no-color.org/).

The CLI flags are global (set once by the top-level Typer callback in
``mship.cli``); env vars are for shell-profile defaults; TTY auto-detection is
the fallback so interactive use and ``| jq`` keep working with no flags.
"""
import json
import os
import sys
from typing import Any, Optional, TextIO

from rich.console import Console
from rich.table import Table

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class _OutputSettings:
    """Process-global flag state, populated once by the CLI callback."""

    __slots__ = ("json", "quiet", "no_color")

    def __init__(self) -> None:
        self.json: Optional[bool] = None
        self.quiet: Optional[bool] = None
        self.no_color: Optional[bool] = None

    def reset(self) -> None:
        self.json = None
        self.quiet = None
        self.no_color = None


_SETTINGS = _OutputSettings()


def configure_output(
    *,
    json: Optional[bool] = None,
    quiet: Optional[bool] = None,
    no_color: Optional[bool] = None,
) -> None:
    """Record CLI-flag intent. Only non-None values override; passing False is a
    deliberate override (e.g. tests), while the Typer callback passes None when a
    flag was not given so env/TTY still decide."""
    if json is not None:
        _SETTINGS.json = json
    if quiet is not None:
        _SETTINGS.quiet = quiet
    if no_color is not None:
        _SETTINGS.no_color = no_color


def reset_output_settings() -> None:
    """Clear global flag state (for tests / repeated in-process invocations)."""
    _SETTINGS.reset()


def _first(*vals: Optional[bool], default: bool) -> bool:
    for v in vals:
        if v is not None:
            return v
    return default


def _env_tristate(name: str) -> Optional[bool]:
    v = os.environ.get(name)
    if v is None:
        return None
    s = v.strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return True  # any other non-empty value reads as "on"


def _env_no_color() -> Optional[bool]:
    # Per no-color.org: presence with a non-empty value disables color,
    # regardless of the value. Absent or empty leaves the decision to others.
    v = os.environ.get("NO_COLOR")
    if not v:
        return None
    return True


class Output:
    """Resolves output shape/color/verbosity once at construction, then renders
    Rich for terminals and JSON for pipes (or whatever the flags force)."""

    def __init__(
        self,
        stream: TextIO | None = None,
        err_stream: TextIO | None = None,
        *,
        force_json: Optional[bool] = None,
        force_quiet: Optional[bool] = None,
        force_no_color: Optional[bool] = None,
    ) -> None:
        self._stream = stream or sys.stdout
        self._err_stream = err_stream or sys.stderr
        self._force_json = force_json
        self._force_quiet = force_quiet
        self._force_no_color = force_no_color

    # ---- resolved state (lazy: derived from is_tty so tests/callers that
    # override is_tty flow through consistently) ----

    @property
    def is_tty(self) -> bool:
        """Raw terminal check. Use for *interactivity* (prompts/confirmations),
        not output shape — for shape use ``human_mode``/``json_mode``."""
        return hasattr(self._stream, "isatty") and self._stream.isatty()

    @property
    def json_mode(self) -> bool:
        return _first(
            self._force_json, _SETTINGS.json, _env_tristate("MSHIP_JSON"),
            default=not self.is_tty,
        )

    @property
    def human_mode(self) -> bool:
        """True when output should be human/Rich rather than JSON.

        Exactly the negation of json_mode: the TTY dependency already lives in
        json_mode's default (a pipe defaults to JSON), so forcing JSON *off*
        (e.g. MSHIP_JSON=0) yields human output even on a pipe rather than a
        silent JSON fallback in table()/print().
        """
        return not self.json_mode

    @property
    def quiet(self) -> bool:
        return _first(
            self._force_quiet, _SETTINGS.quiet, _env_tristate("MSHIP_QUIET"),
            default=False,
        )

    @property
    def no_color(self) -> bool:
        return _first(
            self._force_no_color, _SETTINGS.no_color, _env_no_color(), default=False
        )

    @property
    def use_color(self) -> bool:
        """Color only in human mode, and only unless explicitly stripped."""
        return self.human_mode and not self.no_color

    # Color follows Rich's own terminal detection (so it never injects ANSI into
    # a non-terminal capture / pipe); ``no_color`` force-strips it when the user
    # asked (--no-color / --json / NO_COLOR). We don't force_terminal: in real use
    # human_mode already implies a real TTY, so Rich colorizes on its own. Plain
    # (uncached) properties so the console always reflects the currently resolved
    # color — no dependency on Output-construction vs. configure_output ordering.
    @property
    def _console(self) -> Console:
        return Console(file=self._stream, no_color=not self.use_color)

    @property
    def _err_console(self) -> Console:
        return Console(file=self._err_stream, stderr=True, no_color=not self.use_color)

    # ---- rendering ----

    def json(self, data: dict[str, Any]) -> None:
        self._stream.write(json.dumps(data, indent=2, default=str) + "\n")

    def warning(self, message: str) -> None:
        if self.quiet:
            return
        if self.human_mode:
            self._console.print(f"[yellow]WARNING:[/yellow] {message}")
        else:
            # Non-human: warnings go to stderr so they never corrupt the JSON
            # payload on stdout (e.g. `mship phase dev | jq`). JSON-mode
            # consumers that need the warnings get them in-band via the
            # payload's `warnings` field. (MOS-177)
            self._err_stream.write(f"WARNING: {message}\n")

    def error(self, message: str) -> None:
        # Errors are never suppressed by --quiet.
        if self.human_mode:
            self._err_console.print(f"[red]ERROR:[/red] {message}")
        else:
            self._err_stream.write(f"ERROR: {message}\n")

    def success(self, message: str) -> None:
        if self.human_mode:
            self._console.print(f"[green]{message}[/green]")
        else:
            self._stream.write(f"{message}\n")

    def table(self, title: str, columns: list[str], rows: list[list[str]]) -> None:
        if self.human_mode:
            t = Table(title=title)
            for col in columns:
                t.add_column(col)
            for row in rows:
                t.add_row(*row)
            self._console.print(t)
        else:
            self.json({"title": title, "columns": columns, "rows": rows})

    def breadcrumb(self, message: str) -> None:
        """Dim informational line to stderr. Used for task-resolution breadcrumbs.

        Suppressed in --quiet and whenever output isn't human (JSON-mode
        consumers should attach the same info as structured fields in their
        payload). Stderr so it doesn't corrupt stdout pipes; dim so it doesn't
        compete with real output.
        """
        if self.quiet:
            return
        if self.human_mode:
            self._err_console.print(f"[dim]{message}[/dim]")

    def print(self, message: str) -> None:
        if self.human_mode:
            self._console.print(message)
        else:
            self._stream.write(f"{message}\n")
