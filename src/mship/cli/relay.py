"""`mship relay` sub-app — manage the reverse-tunnel relay client side.

`mship relay setup` generates (if absent) a dedicated ed25519 key used to open
the reverse tunnel, and prints its public key for allow-listing on the relay
host (`docker/relay/pubkeys/`). See plan Task B7 (ac4).
"""
from __future__ import annotations

import typer

from mship.cli.output import Output


def register(parent: typer.Typer, get_container):
    relay_app = typer.Typer(
        name="relay",
        help="Manage the mship reverse-tunnel relay client (keys).",
        no_args_is_help=True,
    )

    @relay_app.command("setup")
    def setup():
        """Generate the relay SSH key (if absent) and print its public key to allow-list."""
        from pathlib import Path

        from mship.core.relay.keys import ensure_relay_key, relay_public_key

        output = Output()
        key_path = ensure_relay_key(home=Path.home())
        pub = relay_public_key(key_path).strip()

        output.print(pub)
        output.print(
            "\nAdd the line above to your relay's `docker/relay/pubkeys/` directory "
            "(one file per key), then restart the relay, to allow this machine to "
            "open tunnels."
        )

    parent.add_typer(relay_app)
