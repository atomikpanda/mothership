"""`mship ui` — open the serve-host management console.

The console lives behind the serve bearer, which a browser cannot send from an
address bar, so it is reached by visiting `/ui?token=<serve token>` once (the
console swaps that for a short-lived cookie and cleans the URL). Nobody should
have to assemble that URL by hand — this command does it.

Opens a browser when there is one. Otherwise it prints the link and offers `c` to
copy it, which is the common case: the workspace is usually on a headless box or
reached over ssh, where the URL has to travel to the human's own machine.
"""
from __future__ import annotations

import typer

from mship.cli.output import Output


def _has_display() -> bool:
    """Whether a browser could plausibly open here.

    `webbrowser.open` returns True on Linux even with no display (it "succeeds"
    by handing off to a launcher that then fails), so the environment is checked
    first rather than trusting the return value.
    """
    import os
    import shutil
    import sys

    if sys.platform == "darwin" or os.name == "nt":
        return True
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False          # a browser here would open on the wrong machine
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return shutil.which("xdg-open") is not None


def _copy_to_clipboard(text: str) -> str | None:
    """Copy `text`, returning the mechanism used, or None if none worked.

    Tries the local clipboard tools first, then falls back to OSC 52 — the
    terminal escape that carries a copy request over ssh to the machine the human
    is actually sitting at, which is the same mechanism the Textual views use.
    """
    import base64
    import shutil
    import subprocess
    import sys

    for argv, name in (
        (["pbcopy"], "pbcopy"),                        # macOS
        (["wl-copy"], "wl-copy"),                      # Wayland
        (["xclip", "-selection", "clipboard"], "xclip"),
        (["xsel", "--clipboard", "--input"], "xsel"),
    ):
        if shutil.which(argv[0]) is None:
            continue
        try:
            subprocess.run(argv, input=text.encode(), check=True, timeout=5)
            return name
        except (OSError, subprocess.SubprocessError):
            continue

    # OSC 52: works through ssh and tmux, but only when stdout is a terminal.
    if sys.stdout.isatty():
        payload = base64.b64encode(text.encode()).decode()
        sys.stdout.write(f"\033]52;c;{payload}\a")
        sys.stdout.flush()
        return "terminal (OSC 52)"
    return None


def _read_one_key() -> str:
    """Read a single keypress without requiring Enter. Returns "" when stdin is
    not a terminal (piped/CI), so the caller just skips the prompt."""
    import sys

    if not sys.stdin.isatty():
        return ""
    try:
        import termios
        import tty
    except ImportError:                      # pragma: no cover - non-POSIX
        return sys.stdin.read(1)
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def console_url(host: str, port: int, token: str | None) -> str:
    """The URL that opens the console, carrying the one-time token when the serve
    requires auth. Kept separate from the command so it can be tested directly."""
    base = f"http://{host}:{port}/ui"
    return f"{base}?token={token}" if token else base


def register(app: typer.Typer, get_container):
    @app.command(rich_help_panel="Inspection")
    def ui(
        host: str = typer.Option("127.0.0.1", "--host", help="Host the serve is bound to."),
        port: int = typer.Option(47100, "--port", help="Port the serve is bound to."),
        no_browser: bool = typer.Option(
            False, "--no-browser", help="Never open a browser; just print the link."
        ),
    ):
        """Open the serve-host management console (topology + setup commands)."""
        import os
        from pathlib import Path

        out = Output()
        container = get_container()

        # Same precedence as `relay.token.ensure_serve_token`, minus the
        # generate-on-absence step: env override, then the persisted file. None
        # means the serve runs without auth (tokenless loopback) and needs no
        # token in the URL.
        token = os.environ.get("MSHIP_SERVE_TOKEN")
        if not token:
            # `container.state_dir()` — not `config_path().parent/.mothership` —
            # because state is anchored to the MAIN checkout when you are inside a
            # git worktree, which is exactly where an agent would run this from.
            token_file = Path(container.state_dir()) / "serve-token"
            try:
                token = token_file.read_text().strip() or None
            except (OSError, ValueError):
                # ValueError covers UnicodeDecodeError on a corrupt token file.
                token = None

        url = console_url(host, port, token)

        if out.json_mode:
            out.json({"url": url, "requires_token": token is not None})
            return

        if not no_browser and _has_display():
            import webbrowser

            if webbrowser.open(url):
                out.success(f"opened the console at http://{host}:{port}/ui")
                return
            out.warning("could not open a browser; here is the link instead")

        # No browser here — print the link. The token is in it, so say what that
        # means rather than leaving the operator to guess.
        out.print(f"\n  [bold]{url}[/bold]\n")
        if token:
            out.print(
                "  [dim]The token in that link is exchanged for a short-lived "
                "cookie on first load.[/dim]"
            )
        out.print("  [dim]Press [bold]c[/bold] to copy, any other key to skip.[/dim]")

        if _read_one_key().lower() == "c":
            mechanism = _copy_to_clipboard(url)
            if mechanism:
                out.success(f"copied to the clipboard via {mechanism}")
            else:
                out.warning("no clipboard mechanism available — copy the link above")
