from __future__ import annotations

from pathlib import Path


def create_app(
    specs_dir: Path,
    state_manager,
    log_manager,
    workspace_root: Path,
    workspace_name: str = "mothership",
):
    """Build the read-only mship serve FastAPI app. Sync handlers call the core
    directly; FastAPI serializes the returns (pydantic models, dicts, dataclasses)."""
    from fastapi import FastAPI, HTTPException  # noqa: F401  (HTTPException used in later tasks)

    from mship.core.spec_store import SpecStore

    app = FastAPI(title="mship serve", version="0")
    store = SpecStore(specs_dir)  # noqa: F841  (used in later tasks)

    @app.get("/health")
    def health():
        return {"status": "ok", "workspace": workspace_name}

    return app
