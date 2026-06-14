from __future__ import annotations

from pathlib import Path

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
    ):
        """Run a read-only JSON API over the spec + task model (Ground Control)."""
        import os
        import uvicorn
        from mship.core.serve import create_app
        from mship.core.spec_store import SPECS_DIRNAME

        output = Output()
        token = os.environ.get("MSHIP_SERVE_TOKEN")
        loopback = {"127.0.0.1", "localhost", "::1"}
        if host not in loopback and not token:
            output.error(
                f"Refusing to bind to non-loopback host {host!r} without auth. "
                f"Set MSHIP_SERVE_TOKEN to expose the API safely."
            )
            raise typer.Exit(1)

        container = get_container()
        workspace_root = Path(container.config_path()).parent
        api = create_app(
            specs_dir=workspace_root / SPECS_DIRNAME,
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            workspace_root=workspace_root,
            workspace_name=container.config().workspace,
            auth_token=token,
        )
        auth_note = "auth: bearer token" if token else "auth: none (loopback only)"
        output.print(f"mship serve → http://{host}:{port}  ({auth_note}; docs: /docs)")
        uvicorn.run(api, host=host, port=port)
