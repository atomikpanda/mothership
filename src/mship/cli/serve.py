from __future__ import annotations

from pathlib import Path

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def serve(
        port: int = typer.Option(47100, "--port", help="Port to bind on 127.0.0.1."),
    ):
        """Run a read-only JSON API over the spec + task model (Ground Control)."""
        import uvicorn
        from mship.core.serve import create_app
        from mship.core.spec_store import SPECS_DIRNAME

        container = get_container()
        workspace_root = Path(container.config_path()).parent
        api = create_app(
            specs_dir=workspace_root / SPECS_DIRNAME,
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            workspace_root=workspace_root,
            workspace_name=container.config().workspace,
        )
        Output().print(f"mship serve → http://127.0.0.1:{port}  (docs: /docs)")
        uvicorn.run(api, host="127.0.0.1", port=port)
