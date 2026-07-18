"""`mship pair` — print a scannable pairing deep-link + QR for the Ground Control app.

Resolves the workspace's relay host with a fixed precedence — an explicit
`--relay-host` flag > a `relay:` block in mothership.yaml > the runtime record of a
live `mship serve --relay-host <host>` in this workspace — then builds the SAME
`groundcontrol://add?…` deep-link the serve prints (via `build_relay_pair_link`) and
renders a terminal QR. Exits non-zero with an actionable message when nothing
resolves. See spec mship-pair-relay-host.
"""
from __future__ import annotations

from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(rich_help_panel="Messaging")
    def pair(
        relay_host: Optional[str] = typer.Option(
            None,
            "--relay-host",
            metavar="HOST",
            show_default=False,
            help="Relay host for the pairing link. Overrides config.relay.host and "
                 "any running-serve record (precedence: flag > config > live serve).",
        ),
    ):
        """Print a pairing deep-link + QR to connect the Ground Control app to this workspace."""
        from pathlib import Path

        import segno

        from mship.core.relay.link import build_relay_pair_link
        from mship.core.relay.runtime import live_runtime_record, resolve_relay

        output = Output()
        container = get_container()
        config = container.config()
        workspace = config.workspace
        workspace_root = Path(container.config_path()).parent

        record = live_runtime_record(workspace_root)  # None if absent or pid dead (stale)
        resolved = resolve_relay(
            flag_host=relay_host,
            config_relay=config.relay,
            record=record,
        )
        if resolved is None:
            output.error(
                "No relay to pair with. Pass `mship pair --relay-host <host>`, add a "
                "`relay:` block (host) to mothership.yaml, or start "
                "`mship serve --relay-host <host>` in this workspace first. "
                "See docs/relay-hosting.md."
            )
            raise typer.Exit(1)

        result = build_relay_pair_link(
            workspace=workspace,
            host=resolved.host,
            workspace_root=workspace_root,
            home=Path.home(),
        )

        output.print(result.link)
        output.print(
            "  (opaque subdomain — no workspace name leaked; if you upgraded mship, "
            "this changed, so re-scan to re-pair. Decode with `mship relay whoami <sub>`.)"
        )
        typer.echo(segno.make(result.link).terminal(compact=True))
