"""`mship relay` sub-app — manage the reverse-tunnel relay client side.

`mship relay setup` generates (if absent) a dedicated ed25519 key used to open
the reverse tunnel, and prints its public key for allow-listing on the relay
host (`docker/relay/pubkeys/`). See plan Task B7 (ac4).

`mship relay enroll-server` runs the public enroll HTTP endpoint on the relay
host; `mship relay requests/approve/deny` manage pending enroll requests
(owner-side); `mship relay enroll` is the requester path (new device).
"""
from __future__ import annotations

import os

import typer

from mship.cli.output import Output


def enroll_base_url(*, enroll_url, relay_host, config_host):
    """Resolve the enroll endpoint base URL. Precedence:
    explicit --enroll-url  >  --relay-host  >  configured relay.host.
    A relay host derives https://enroll.<host>."""
    if enroll_url:
        return enroll_url.rstrip("/")
    host = relay_host or config_host
    if not host:
        raise ValueError("provide --relay-host (or configure relay.host)")
    return f"https://enroll.{host.strip().rstrip('.')}"


def _configured_relay_host(get_container):
    """Best-effort: the workspace's configured relay.host, or None outside a workspace."""
    try:
        rc = get_container().config().relay
        return rc.host if rc else None
    except Exception:
        return None


def _run_uvicorn(app, host, port):   # seam so tests don't boot a server
    import uvicorn
    uvicorn.run(app, host=host, port=port)


def _enroll_server_impl(*, store_dir, pubkeys_dir, port, host, ttl, relay_domain):
    from pathlib import Path

    from mship.core.relay.enroll import RequestStore
    from mship.core.relay.enroll_app import build_enroll_app

    if not relay_domain:
        raise typer.BadParameter("relay domain required: pass --relay-domain or set RELAY_DOMAIN")
    store = RequestStore(Path(store_dir), ttl_seconds=ttl)
    Output().print(f"enroll-server → http://{host}:{port}  (relay: {relay_domain}, "
                   f"pubkeys: {pubkeys_dir}, store: {store_dir}, ttl: {ttl}s)")
    _run_uvicorn(build_enroll_app(store, relay_domain=relay_domain), host=host, port=port)


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
        host: str = typer.Option("127.0.0.1", "--host",
                                 help="Interface to bind (loopback; Caddy fronts it)."),
        ttl: int = typer.Option(1800, "--ttl",
                                help="Pending request TTL in seconds (default 30 min)."),
        relay_domain: str = typer.Option(lambda: os.environ.get("RELAY_DOMAIN", ""), "--relay-domain",
                                         help="Relay domain for the on-demand TLS ask (default $RELAY_DOMAIN)."),
    ):
        """Run the public enroll endpoint on the relay host (devices POST their key here)."""
        _enroll_server_impl(store_dir=store_dir, pubkeys_dir=pubkeys_dir,
                            port=port, host=host, ttl=ttl, relay_domain=relay_domain)

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
        enroll_url: str = typer.Option(None, "--enroll-url",
                                       help="Explicit enroll base URL (overrides --relay-host)."),
        relay_host: str = typer.Option(None, "--relay-host",
                                       help="Relay host, e.g. mship-relay.example.com (enroll URL is derived)."),
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
        try:
            base = enroll_base_url(enroll_url=enroll_url, relay_host=relay_host,
                                   config_host=_configured_relay_host(get_container))
        except ValueError as e:
            out.error(str(e)); raise typer.Exit(2)
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
        try:
            rid = r.json()["id"]
        except (ValueError, KeyError):
            out.error("enroll server returned an unexpected response (no request id)")
            raise typer.Exit(1)
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

    parent.add_typer(relay_app, rich_help_panel="Setup")
