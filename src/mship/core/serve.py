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
    from fastapi import FastAPI, HTTPException

    from mship.core.spec_store import SpecStore

    app = FastAPI(title="mship serve", version="0")
    store = SpecStore(specs_dir)

    @app.get("/health")
    def health():
        return {"status": "ok", "workspace": workspace_name}

    from mship.core.spec_review import build_review

    @app.get("/specs")
    def list_specs():
        return [
            {"id": s.id, "title": s.title, "status": s.status, "task_slug": s.task_slug}
            for s in store.list()
        ]

    @app.get("/specs/{spec_id}")
    def get_spec(spec_id: str):
        spec = store.find_by_id(spec_id)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"no spec {spec_id!r}")
        return spec.model_dump(mode="json")

    @app.get("/specs/{spec_id}/review")
    def get_review(spec_id: str):
        spec = store.find_by_id(spec_id)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"no spec {spec_id!r}")
        return build_review(spec)

    return app
