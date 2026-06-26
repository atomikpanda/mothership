from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def serve(
        host: str = typer.Option(
            "127.0.0.1", "--host",
            help="Bind address. Use your tailnet IP (or 0.0.0.0) to reach it from "
                 "other devices — requires MSHIP_SERVE_TOKEN.",
        ),
        port: int = typer.Option(47100, "--port", help="Port."),
        relay: bool = typer.Option(
            False, "--relay",
            help="Expose this workspace via a reverse SSH tunnel to a relay host. "
                 "Uses config.relay.host unless --relay-host overrides it. Serve "
                 "stays bound to loopback; a bearer token is required (auto-generated).",
        ),
        relay_host: Optional[str] = typer.Option(
            None, "--relay-host",
            metavar="HOST",
            help="Relay host to tunnel through (implies --relay). Overrides config.relay.host.",
            show_default=False,
        ),
        relay_tick: float = typer.Option(
            1.0, "--relay-tick", hidden=True,
            help="Seconds between tunnel-supervisor health checks.",
        ),
    ):
        """Run a JSON API over the spec + task model — reads plus review/approve writes (Ground Control)."""
        import os

        # Relaying is on if --relay is given OR a --relay-host override is supplied.
        relay_enabled = relay or relay_host is not None
        relay_host_override = relay_host

        output = Output()
        container = get_container()
        workspace_root = Path(container.config_path()).parent
        config = container.config()

        if relay_enabled:
            _serve_with_relay(
                container=container,
                config=config,
                workspace_root=workspace_root,
                output=output,
                relay_host_override=relay_host_override,
                port=port,
                relay_tick=relay_tick,
            )
            return

        # ---- existing (non-relay) behavior ----
        import uvicorn
        from mship.core.serve import create_app
        from mship.core.spec_store import SPECS_DIRNAME

        token = os.environ.get("MSHIP_SERVE_TOKEN")
        loopback = {"127.0.0.1", "localhost", "::1"}
        if host not in loopback and not token:
            output.error(
                f"Refusing to bind to non-loopback host {host!r} without auth. "
                f"Set MSHIP_SERVE_TOKEN to expose the API safely."
            )
            raise typer.Exit(1)

        api = create_app(
            specs_dir=workspace_root / SPECS_DIRNAME,
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            workspace_root=workspace_root,
            workspace_name=config.workspace,
            auth_token=token,
            worktree_manager=container.worktree_manager(),
        )
        auth_note = "auth: bearer token" if token else "auth: none (loopback only)"
        docs_note = "docs: disabled (auth)" if token else "docs: /docs"
        output.print(f"mship serve → http://{host}:{port}  ({auth_note}; {docs_note})")

        from mship.core.serve_pair import serve_pair_link
        pair = serve_pair_link(host, port, token, config.workspace)
        if pair is not None:
            import segno
            from mship.core.relay.pairing import parse_pair_link
            advertised_url = parse_pair_link(pair)["url"]  # exactly what the QR encodes
            output.print(f"pair → {pair}")
            output.print(f"  {advertised_url}  (plain HTTP — fine on a trusted LAN or tailnet "
                         "(WireGuard-encrypted); use --relay for untrusted networks)")
            typer.echo(segno.make(pair).terminal(compact=True))
        elif token and host in {"0.0.0.0", "::"}:
            output.print(
                "  (couldn't determine a LAN/tailnet IP for a pairing QR; "
                "pass --host <your-ip> to print one)"
            )

        uvicorn.run(api, host=host, port=port)


def _serve_with_relay(
    *,
    container,
    config,
    workspace_root: Path,
    output: Output,
    relay_host_override: Optional[str],
    port: int,
    relay_tick: float,
):
    """Run uvicorn on loopback while supervising a reverse SSH tunnel to the relay.

    Token is REQUIRED when relaying (the public URL is reachable from anywhere);
    it is taken from MSHIP_SERVE_TOKEN if set, else auto-generated + persisted.
    uvicorn blocks in the main thread; the tunnel supervisor is ticked from a
    background daemon thread on an interval and torn down on shutdown.
    """
    import threading

    import segno
    import uvicorn

    from mship.core.relay.config import RelayConfig
    from mship.core.relay.health import wait_until_reachable
    from mship.core.relay.keys import ensure_relay_key, relay_public_key
    from mship.core.relay.pairing import build_pair_link
    from mship.core.relay.token import ensure_serve_token
    from mship.core.relay.tunnel import (
        TunnelSupervisor,
        build_tunnel_argv,
        device_id,
        device_subdomain,
    )
    from mship.core.serve import create_app
    from mship.core.spec_store import SPECS_DIRNAME

    # Resolve the relay config: an explicit --relay <host> overrides config.relay.host;
    # otherwise fall back to the configured relay block entirely.
    rc = config.relay
    if relay_host_override:
        if rc is not None:
            rc = RelayConfig(host=relay_host_override, ssh_port=rc.ssh_port, user=rc.user)
        else:
            rc = RelayConfig(host=relay_host_override)
    if rc is None:
        output.error(
            "--relay requires a relay host. Pass it (`mship serve --relay relay.example.com`) "
            "or add a `relay:` block (host) to mothership.yaml. See docs/relay-hosting.md."
        )
        raise typer.Exit(1)

    workspace = config.workspace

    # Token is REQUIRED when relaying — the public URL is reachable from anywhere.
    token = ensure_serve_token(workspace_root)

    # serve stays bound to loopback; the tunnel is what exposes it.
    host = "127.0.0.1"
    api = create_app(
        specs_dir=workspace_root / SPECS_DIRNAME,
        state_manager=container.state_manager(),
        log_manager=container.log_manager(),
        workspace_root=workspace_root,
        workspace_name=workspace,
        auth_token=token,
        worktree_manager=container.worktree_manager(),
    )

    key_path = ensure_relay_key(home=Path.home())
    dev = device_id(relay_public_key(key_path))
    subdomain = device_subdomain(workspace, dev)          # was: subdomain_for(workspace)
    argv = build_tunnel_argv(rc, subdomain=subdomain, local_port=port, key_path=key_path)

    public_url = f"https://{subdomain}.{rc.host}"
    link = build_pair_link(url=public_url, token=token, workspace=workspace)

    log_path = workspace_root / ".mothership" / "relay-tunnel.log"
    log_path.unlink(missing_ok=True)                      # fresh per run
    sup = TunnelSupervisor(argv=argv, log_path=log_path)
    sup.start()

    # Background daemon thread ticks the supervisor (reconnect on drop) while
    # uvicorn blocks the main thread. Daemon so it never blocks interpreter exit.
    stop_event = threading.Event()

    import time

    def _tick_loop():
        warned = False
        while not stop_event.wait(relay_tick):
            try:
                sup.tick()
                if sup.restart_count >= 3 and not warned:
                    warned = True
                    output.error(
                        "relay tunnel keeps dropping (restarted "
                        f"{sup.restart_count}×). Last ssh output:\n"
                        + sup.recent_output().strip()
                    )
            except Exception:
                pass

    def _verify_loop():
        # wait for uvicorn to answer locally, then probe the PUBLIC url end-to-end.
        import httpx
        local = f"http://{host}:{port}/health"
        deadline = time.monotonic() + 30
        local_up = False
        while time.monotonic() < deadline and not stop_event.is_set():
            try:
                httpx.get(local, headers={"Authorization": f"Bearer {token}"}, timeout=2)
                local_up = True
                break
            except Exception:
                time.sleep(0.5)
        if stop_event.is_set():
            return                      # clean shutdown — don't emit a spurious ✗
        if not local_up:
            output.error("✗ local server didn't come up within 30s; relay not verified")
            return
        # The tunnel route, TLS cert, and DNS can take a few seconds after
        # startup — retry transient failures before declaring the relay down.
        ok, detail = wait_until_reachable(public_url, token)
        if stop_event.is_set():
            return                      # shut down while the probe was in-flight — no spurious ✗
        if ok:
            output.success(f"✓ relay reachable: {public_url}")
        else:
            tail = sup.recent_output().strip()
            output.error(f"✗ relay NOT reachable: {detail}"
                         + (f"\nssh tunnel output:\n{tail}" if tail else ""))

    ticker = threading.Thread(target=_tick_loop, name="mship-relay-tick", daemon=True)
    ticker.start()
    threading.Thread(target=_verify_loop, name="mship-relay-verify", daemon=True).start()

    output.print(f"mship serve → http://{host}:{port}  (auth: bearer token; docs: disabled)")
    output.print(f"relay → {public_url}  (per-device; tunnel via ssh -R to {rc.host})")
    output.print(link)
    typer.echo(segno.make(link).terminal(compact=True))

    try:
        uvicorn.run(api, host=host, port=port)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        sup.stop()
