"""`mship pair` — print a scannable pairing deep-link + QR for the Ground Control app.

Resolves the workspace's relay config, computes the public URL + serve token,
builds a `groundcontrol://add?...` deep-link, prints it, and renders a terminal
QR code so the phone app can scan it without typing. See plan Task B7 (ac6).
"""
from __future__ import annotations

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(rich_help_panel="Messaging")
    def pair():
        """Print a pairing deep-link + QR to connect the Ground Control app to this workspace."""
        from pathlib import Path

        import segno

        from mship.core.relay.keys import (
            ensure_relay_key,
            ensure_subdomain_secret,
            relay_public_key,
        )
        from mship.core.relay.pairing import build_pair_link
        from mship.core.relay.token import ensure_serve_token
        from mship.core.relay.tunnel import device_id, device_subdomain

        output = Output()
        container = get_container()
        config = container.config()
        rc = config.relay
        if rc is None:
            output.error(
                "No relay configured. Add a `relay:` block (host) to mothership.yaml "
                "to enable phone pairing. See docs/relay-hosting.md."
            )
            raise typer.Exit(1)

        workspace = config.workspace
        workspace_root = Path(container.config_path()).parent
        key_path = ensure_relay_key(home=Path.home())
        secret = ensure_subdomain_secret(home=Path.home())
        subdomain = device_subdomain(workspace, device_id(relay_public_key(key_path)), secret)
        url = f"https://{subdomain}.{rc.host}"
        token = ensure_serve_token(workspace_root)
        link = build_pair_link(url=url, token=token, workspace=workspace)

        output.print(link)
        typer.echo(segno.make(link).terminal(compact=True))
