"""`mship relay` sub-app — manage the reverse-tunnel relay client side.

`mship relay setup` generates (if absent) a dedicated ed25519 key used to open
the reverse tunnel, and prints its public key for allow-listing on the relay
host (`docker/relay/pubkeys/`). See plan Task B7 (ac4).

`mship relay enroll-server` runs the public enroll HTTP endpoint on the relay
host; `mship relay requests/approve/deny` manage pending enroll requests
(owner-side); `mship relay enroll` is the requester path (new device).
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

    # -------------------------------------------------------------------------
    # Owner-side: enroll-server
    # -------------------------------------------------------------------------

    @relay_app.command("enroll-server")
    def enroll_server(
        pubkeys_dir: str = typer.Option("./pubkeys", "--pubkeys-dir",
                                        help="Allowlist dir used by 'mship relay approve' "
                                             "(this server only creates pending requests; "
                                             "it never writes keys)."),
        store_dir: str = typer.Option("./pending-store", "--store-dir",
                                      help="Directory for pending enroll request state."),
        port: int = typer.Option(47180, "--port", help="Port to listen on."),
        host: str = typer.Option("0.0.0.0", "--host", help="Interface to bind."),
        ttl: int = typer.Option(1800, "--ttl",
                                help="Pending request TTL in seconds (default 30 min)."),
    ):
        """Run the public enroll endpoint on the relay host (devices POST their key here)."""
        import uvicorn
        from pathlib import Path

        from mship.core.relay.enroll import RequestStore
        from mship.core.relay.enroll_app import build_enroll_app

        out = Output()
        store = RequestStore(Path(store_dir), ttl_seconds=ttl)
        out.print(
            f"enroll-server → http://{host}:{port}  "
            f"(pubkeys: {pubkeys_dir}, store: {store_dir}, ttl: {ttl}s)"
        )
        uvicorn.run(build_enroll_app(store), host=host, port=port)

    # -------------------------------------------------------------------------
    # Owner-side: list / approve / deny
    # -------------------------------------------------------------------------

    @relay_app.command("requests")
    def requests_cmd(
        store_dir: str = typer.Option("./pending-store", "--store-dir",
                                      help="Directory for pending enroll request state."),
    ):
        """List pending enroll requests (id · hostname · fingerprint)."""
        from pathlib import Path

        from mship.core.relay.enroll import RequestStore

        out = Output()
        pending = RequestStore(Path(store_dir)).list_pending()
        if not pending:
            out.print("no pending requests")
            return
        for rec in pending:
            out.print(f"{rec['id']}  {rec['hostname'] or '-'}  {rec['fingerprint']}")

    @relay_app.command("approve")
    def approve_cmd(
        rid: str = typer.Argument(..., help="Request ID to approve."),
        store_dir: str = typer.Option("./pending-store", "--store-dir",
                                      help="Directory for pending enroll request state."),
        pubkeys_dir: str = typer.Option("./pubkeys", "--pubkeys-dir",
                                        help="Directory where approved keys are written (sish pubkeys/)."),
    ):
        """Approve a pending request: add its key to the allowlist (sish picks it up, no restart)."""
        from pathlib import Path

        from mship.core.relay.enroll import NotPending, RequestStore

        out = Output()
        try:
            RequestStore(Path(store_dir)).approve(rid, Path(pubkeys_dir))
            out.success(f"approved {rid} — key enrolled into {pubkeys_dir}")
        except NotPending:
            out.error(f"no pending request {rid!r} (unknown, already resolved, or expired)")
            raise typer.Exit(1)

    @relay_app.command("deny")
    def deny_cmd(
        rid: str = typer.Argument(..., help="Request ID to deny."),
        store_dir: str = typer.Option("./pending-store", "--store-dir",
                                      help="Directory for pending enroll request state."),
    ):
        """Deny a pending request (does not touch the allowlist)."""
        from pathlib import Path

        from mship.core.relay.enroll import NotPending, RequestStore

        out = Output()
        try:
            RequestStore(Path(store_dir)).deny(rid)
            out.print(f"denied {rid}")
        except NotPending:
            out.error(f"no pending request {rid!r} (unknown, already resolved, or expired)")
            raise typer.Exit(1)

    # -------------------------------------------------------------------------
    # Requester-side: enroll (new device)
    # -------------------------------------------------------------------------

    @relay_app.command("enroll")
    def enroll_cmd(
        enroll_url: str = typer.Option(..., "--enroll-url",
                                       help="Base URL of the relay enroll server, e.g. http://<host>:47180."),
        wait: bool = typer.Option(True, "--wait/--no-wait",
                                  help="Poll until approved/denied (default) or return immediately."),
    ):
        """From a NEW device: request relay access; the relay owner approves/denies."""
        import socket
        import time
        from pathlib import Path

        import httpx

        from mship.core.relay.keys import ensure_relay_key, relay_public_key

        out = Output()
        pub = relay_public_key(ensure_relay_key(home=Path.home())).strip()
        base = enroll_url.rstrip("/")
        # The requester runs on a remote device hitting a public endpoint, so
        # connection-refused / DNS / timeout are LIKELY: surface them cleanly
        # rather than dumping an httpx traceback.
        try:
            r = httpx.post(
                f"{base}/enroll",
                json={"pubkey": pub, "hostname": socket.gethostname()},
                timeout=10,
            )
        except httpx.RequestError as exc:
            out.error(f"could not reach enroll server at {base}: {exc}")
            raise typer.Exit(1)
        if r.status_code != 200:
            out.error(f"enroll request failed: HTTP {r.status_code} {r.text}")
            raise typer.Exit(1)
        rid = r.json()["id"]
        out.print(f"requested (id {rid}) — ask the relay owner to run: mship relay approve {rid}")
        if not wait:
            return
        deadline = time.monotonic() + 1800
        try:
            while time.monotonic() < deadline:
                # A transient blip or a non-JSON proxy page (e.g. a 502 error
                # page) shouldn't kill the wait — back off and retry; the
                # deadline still bounds the loop.
                try:
                    resp = httpx.get(f"{base}/status/{rid}", timeout=10)
                    st = resp.json().get("status", "pending")
                except (httpx.RequestError, ValueError):  # ValueError covers JSON decode
                    time.sleep(3)
                    continue
                if st == "approved":
                    out.success("approved — you can now run `mship serve --relay`.")
                    return
                if st in ("denied", "expired"):
                    out.error(f"{st}.")
                    raise typer.Exit(1)
                time.sleep(3)
        except KeyboardInterrupt:
            out.print(
                f"cancelled — your request {rid} is still pending; "
                "the owner can still approve it."
            )
            raise typer.Exit(1)
        out.error("timed out waiting for approval.")
        raise typer.Exit(1)

    parent.add_typer(relay_app)
