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
        """Generate the relay SSH key (if absent) and print a ready-to-run enroll command."""
        import socket
        from pathlib import Path

        from mship.core.relay.keys import ensure_relay_key, relay_public_key

        output = Output()
        key_path = ensure_relay_key(home=Path.home())
        pub_path = Path(str(key_path) + ".pub")
        pub = relay_public_key(key_path).strip()

        # Fill the relay host from config when available so the command is copy-paste ready;
        # otherwise leave a placeholder (setup may run on a fresh device with no workspace).
        relay_host = "<relay-host>"
        try:
            rc = get_container().config().relay
            if rc is not None and getattr(rc, "host", None):
                relay_host = rc.host
        except Exception:
            pass

        label = (socket.gethostname() or "this-device") + ".pub"

        output.print(pub)
        output.print(
            f"\nEnroll this key so this machine can open relay tunnels — drop it in "
            f"`docker/relay/pubkeys/` on the relay host (one file per key; no restart "
            f"needed — sish re-reads the directory per connection):\n\n"
            f"  • On the relay host itself, just copy it in:\n"
            f"      cp {pub_path} <relay-dir>/docker/relay/pubkeys/{label}\n\n"
            f"  • From another machine, scp it over (the tunnel auths by key, so there is\n"
            f"    no \"relay user\" — <login> is just your normal SSH account on the relay box):\n"
            f"      scp {pub_path} <login>@{relay_host}:<relay-dir>/docker/relay/pubkeys/{label}\n\n"
            f"  <relay-dir> = where you deployed the docker/relay/ compose; the filename is "
            f"just a unique label."
        )

    parent.add_typer(relay_app)
