import os
from pathlib import Path

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
            pane size="50%" name="agent"
            pane split_direction="horizontal" size="50%" {
                pane name="specs" command="mship" { args "view" "spec" "--watch"; }
                pane name="status" command="mship" { args "view" "status"; }
            }
        }
    }

    tab name="Dev" {
        pane split_direction="vertical" {
            pane size="60%" name="editor" command="bash" {
                args "-lc" "${EDITOR:-$(command -v nvim || command -v vim || command -v vi)} ."
            }
            pane split_direction="horizontal" size="40%" {
                pane name="logs" command="mship" { args "view" "logs" "--watch"; }
                pane name="status" command="mship" { args "view" "status" "--watch"; }
            }
        }
    }

    tab name="Review" {
        pane split_direction="vertical" {
            pane size="70%" name="diff" command="mship" { args "view" "diff" "--watch"; }
            pane size="30%" name="shell"
        }
    }

    tab name="Run" {
        pane split_direction="vertical" {
            pane size="60%" name="shell"
            pane split_direction="horizontal" size="40%" {
                pane name="logs" command="mship" { args "view" "logs" "--watch"; }
                pane name="status" command="mship" { args "view" "status" "--watch"; }
            }
        }
    }
}
"""


def _target_path() -> Path:
    return Path.home() / ".config" / "zellij" / "layouts" / "mothership.kdl"


def register(app: typer.Typer, get_container):
    layout_app = typer.Typer(name="layout", help="Manage the zellij layout for mothership.", no_args_is_help=True)

    @layout_app.command()
    def init(
        force: bool = typer.Option(False, "--force", help="Overwrite existing layout file."),
    ):
        """Write the mothership zellij layout to ~/.config/zellij/layouts/mothership.kdl."""
        target = _target_path()
        if target.exists() and not force:
            typer.echo(
                f"Error: {target} already exists. Use --force to overwrite.",
                err=True,
            )
            raise typer.Exit(code=1)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_TEMPLATE)
        typer.echo(f"Written: {target}")

    @layout_app.command()
    def launch():
        """Launch zellij with the mothership layout (replaces current process)."""
        os.execvp("zellij", ["zellij", "--layout", "mothership"])

    app.add_typer(layout_app)
