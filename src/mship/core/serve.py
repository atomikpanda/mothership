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

    from fastapi.encoders import jsonable_encoder
    from mship.core.view.task_index import build_task_index

    @app.get("/tasks")
    def list_tasks():
        return jsonable_encoder(build_task_index(state_manager.load(), workspace_root))

    @app.get("/tasks/{slug}")
    def get_task(slug: str):
        state = state_manager.load()
        if slug not in state.tasks:
            raise HTTPException(status_code=404, detail=f"no task {slug!r}")
        by_slug = {t.slug: t for t in build_task_index(state, workspace_root)}
        return jsonable_encoder(by_slug[slug])

    @app.get("/journal/{slug}")
    def get_journal(slug: str):
        state = state_manager.load()
        if slug not in state.tasks:
            raise HTTPException(status_code=404, detail=f"no task {slug!r}")
        return jsonable_encoder(log_manager.read(slug, last=50))

    return app
