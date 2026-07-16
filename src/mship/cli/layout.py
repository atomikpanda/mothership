import os
from pathlib import Path
from typing import Optional

import typer

_TEMPLATE = """\
layout {
    default_tab_template {
        pane size=1 borderless=true {
            plugin location="zellij:tab-bar"
        }
        children
        pane size=2 borderless=true {
            plugin location="zellij:status-bar"
        }
    }

    tab name="Plan" focus=true {
        pane split_direction="vertical" {
            pane size="50%" name="Agent"
            pane split_direction="horizontal" size="50%" {
                pane name="Specs" command="mship" close_on_exit=false { args "view" "spec" "--watch"; }
                pane name="Status" command="mship" close_on_exit=false { args "view" "status" "--watch"; }
            }
        }
    }

    tab name="Dev" {
        pane split_direction="vertical" {
            pane size="60%" name="Editor" command="sh" close_on_exit=false {
                args "-c" "${EDITOR:-$(command -v nvim || command -v vim || command -v vi)} ."
            }
            pane split_direction="horizontal" size="40%" {
                pane name="Journal" command="mship" close_on_exit=false { args "view" "journal" "--watch"; }
                pane name="Status" command="mship" close_on_exit=false { args "view" "status" "--watch"; }
            }
        }
    }

    tab name="Review" {
        pane split_direction="vertical" {
            pane size="70%" name="Diff" command="mship" close_on_exit=false { args "view" "diff" "--watch"; }
            pane size="30%" split_direction="horizontal" {
                pane name="Shell"
                pane name="Journal" command="mship" close_on_exit=false { args "view" "journal" "--watch"; }
            }
        }
    }

    tab name="Run" {
        pane split_direction="vertical" {
            pane size="60%" name="Shell"
            pane split_direction="horizontal" size="40%" {
                pane name="Journal" command="mship" close_on_exit=false { args "view" "journal" "--watch"; }
                pane name="Status" command="mship" close_on_exit=false { args "view" "status" "--watch"; }
            }
        }
    }
}
"""

# Composable parts, sliced from _TEMPLATE at stable markers so the normal layout
# is reconstructed byte-for-byte (_LAYOUT_HEAD + _BASE_TABS + _LAYOUT_TAIL == _TEMPLATE)
# while the serve layout can reuse the base tabs and append a Serve tab. The final
# layout-closing "}" is the only brace at column 0, so `\n}\n` locates it uniquely.
_plan_idx = _TEMPLATE.index('    tab name="Plan"')
_close_idx = _TEMPLATE.rindex("\n}\n") + 1
_LAYOUT_HEAD = _TEMPLATE[:_plan_idx]
_BASE_TABS = _TEMPLATE[_plan_idx:_close_idx]
_LAYOUT_TAIL = _TEMPLATE[_close_idx:]


def serve_cli_args(
    *, host: Optional[str], port: Optional[int], relay: bool, relay_host: Optional[str]
) -> list[str]:
    """Map `mship layout launch` serve options to `mship serve` CLI args, in a stable order."""
    args: list[str] = []
    if host is not None:
        args += ["--host", host]
    if port is not None:
        args += ["--port", str(port)]
    if relay:
        args += ["--relay"]
    if relay_host is not None:
        args += ["--relay-host", relay_host]
    return args


def _kdl_quote(s: str) -> str:
    """Quote a string as a KDL string literal, escaping backslash and double-quote
    so user-supplied values (e.g. --host) can't produce malformed KDL."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _serve_tab(serve_args: list[str]) -> str:
    """The Serve tab KDL block: one pane running `mship serve <serve_args>`."""
    tokens = " ".join(_kdl_quote(a) for a in ["serve", *serve_args])
    return (
        '\n    tab name="Serve" {\n'
        '        pane name="Serve" command="mship" close_on_exit=false { args '
        + tokens
        + "; }\n"
        "    }\n"
    )


def render_serve_layout(serve_args: list[str]) -> str:
    """The serve layout: the normal base tabs plus a Serve tab, as a full KDL document."""
    return _LAYOUT_HEAD + _BASE_TABS + _serve_tab(serve_args) + _LAYOUT_TAIL


def _target_path() -> Path:
    return Path.home() / ".config" / "zellij" / "layouts" / "mothership.kdl"


def _serve_target_path() -> Path:
    return Path.home() / ".config" / "zellij" / "layouts" / "mothership-serve.kdl"


def _serve_launch_path() -> Path:
    """Deterministic per-user path for the launch-time effective serve layout.

    A fixed path (overwritten each launch, outside the layouts/ picker dir) rather
    than a unique tempfile: `os.execvp` replaces the process, so no cleanup runs
    after it — a unique temp would orphan one .kdl per launch. Overwriting one file
    keeps it bounded.
    """
    return Path.home() / ".config" / "zellij" / "mothership-serve-launch.kdl"


def register(app: typer.Typer, get_container):
    layout_app = typer.Typer(name="layout", help="Manage the zellij layout for mothership.", no_args_is_help=True)

    @layout_app.command()
    def init(
        force: bool = typer.Option(False, "--force", help="Overwrite existing layout files."),
    ):
        """Write the mothership zellij layouts (normal + serve) to ~/.config/zellij/layouts/."""
        normal = _target_path()
        serve = _serve_target_path()
        existing = [p for p in (normal, serve) if p.exists()]
        if existing and not force:
            typer.echo(
                "Error: layout file(s) already exist: "
                + ", ".join(str(p) for p in existing)
                + ". Use --force to overwrite.",
                err=True,
            )
            raise typer.Exit(code=1)
        normal.parent.mkdir(parents=True, exist_ok=True)
        normal.write_text(_TEMPLATE)
        serve.write_text(render_serve_layout([]))
        typer.echo(f"Written: {normal}")
        typer.echo(f"Written: {serve}")

    @layout_app.command()
    def launch(
        serve: bool = typer.Option(False, "--serve", help="Open the serve layout (adds a Serve tab running `mship serve`)."),
        host: Optional[str] = typer.Option(None, "--host", help="serve --host (implies --serve)."),
        port: Optional[int] = typer.Option(None, "--port", help="serve --port (implies --serve)."),
        relay: bool = typer.Option(False, "--relay", help="serve --relay (implies --serve). Collides with a separately-running serve."),
        relay_host: Optional[str] = typer.Option(None, "--relay-host", help="serve --relay-host (implies --serve)."),
    ):
        """Launch zellij with the mothership layout (replaces current process).

        With --serve (or any serve flag) launches the serve layout — the normal
        tabs plus a Serve tab running `mship serve <flags>`. WARNING: starting a
        serve here collides with a separately-running `mship serve` (same relay
        subdomain / bind), so use it only when no standalone serve is up.
        """
        serve_selected = (
            serve or host is not None or port is not None or relay or relay_host is not None
        )
        if not serve_selected:
            os.execvp("zellij", ["zellij", "--layout", "mothership"])
            return
        args = serve_cli_args(host=host, port=port, relay=relay, relay_host=relay_host)
        kdl = render_serve_layout(args)
        path = _serve_launch_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(kdl)
        os.execvp("zellij", ["zellij", "--layout", str(path)])

    app.add_typer(layout_app, rich_help_panel="Setup")
