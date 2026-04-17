"""`mship context` — emit a one-shot agent-readable JSON snapshot of workspace state.

See GitHub issue #50. Always emits JSON to stdout (the load-bearing surface for
agents); a `--human` formatter can be added later without breaking the schema.
"""
from __future__ import annotations

from pathlib import Path

import typer

from mship.cli.output import Output
from mship.core.context import build_context


def register(app: typer.Typer, get_container):
    @app.command()
    def context():
        """Emit a JSON snapshot of workspace state for agent consumption."""
        container = get_container()
        payload = build_context(
            state=container.state_manager().load(),
            config=container.config(),
            log_manager=container.log_manager(),
            cwd=Path.cwd(),
        )
        Output().json(payload)
