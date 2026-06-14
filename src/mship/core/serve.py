from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class VerdictBody(BaseModel):
    criterion_id: str
    verdict: str


class QuestionBody(BaseModel):
    text: str


class AnswerBody(BaseModel):
    answer: str


class ApproveBody(BaseModel):
    bypass_gate: bool = False


class ReasonBody(BaseModel):
    reason: str


def _make_auth_dependency(token: str):
    import hmac
    from fastapi import Header, HTTPException

    expected = f"Bearer {token}".encode("utf-8")

    def _require_token(authorization: str | None = Header(default=None)):
        provided = (authorization or "").encode("utf-8")
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    return _require_token


def create_app(
    specs_dir: Path,
    state_manager,
    log_manager,
    workspace_root: Path,
    workspace_name: str = "mothership",
    auth_token: str | None = None,
):
    """Build the read-only mship serve FastAPI app. Sync handlers call the core
    directly; FastAPI serializes the returns (pydantic models, dicts, dataclasses)."""
    from fastapi import Depends, FastAPI, HTTPException

    from mship.core.spec_store import SpecStore

    if auth_token:
        dependencies = [Depends(_make_auth_dependency(auth_token))]
        # Auth covers user routes but NOT FastAPI's built-in docs/openapi routes,
        # so disable them when exposed behind auth (no unauthenticated schema surface).
        app = FastAPI(
            title="mship serve", version="0", dependencies=dependencies,
            docs_url=None, redoc_url=None, openapi_url=None,
        )
    else:
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

    # --- write endpoints ---

    from datetime import datetime, timezone
    from mship.core.spec_review import set_criterion_verdict
    from mship.core.spec_questions import add_question, answer_question

    def _load_or_404(spec_id: str):
        spec = store.find_by_id(spec_id)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"no spec {spec_id!r}")
        return spec

    def _save_and_review(spec):
        spec.updated_at = datetime.now(timezone.utc)
        store.save(spec)
        return build_review(spec)

    @app.post("/specs/{spec_id}/verdict")
    def post_verdict(spec_id: str, body: VerdictBody):
        spec = _load_or_404(spec_id)
        try:
            set_criterion_verdict(spec, body.criterion_id, body.verdict)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _save_and_review(spec)

    @app.post("/specs/{spec_id}/questions")
    def post_question(spec_id: str, body: QuestionBody):
        spec = _load_or_404(spec_id)
        add_question(spec, body.text)
        return _save_and_review(spec)

    @app.post("/specs/{spec_id}/questions/{qid}/answer")
    def post_answer(spec_id: str, qid: str, body: AnswerBody):
        spec = _load_or_404(spec_id)
        try:
            answer_question(spec, qid, body.answer)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return _save_and_review(spec)

    from mship.core.spec import InvalidTransition, validate_transition
    from mship.core.spec_approve import approval_blockers

    @app.post("/specs/{spec_id}/approve")
    def post_approve(spec_id: str, body: ApproveBody):
        spec = _load_or_404(spec_id)
        if not body.bypass_gate:
            blockers = approval_blockers(spec)
            if blockers:
                raise HTTPException(status_code=409, detail={"blocked": blockers})
        try:
            validate_transition(spec.status, "approved")
        except InvalidTransition as e:
            raise HTTPException(status_code=409, detail=str(e))
        spec.status = "approved"
        return _save_and_review(spec)

    @app.post("/specs/{spec_id}/request-changes")
    def post_request_changes(spec_id: str, body: ReasonBody):
        spec = _load_or_404(spec_id)
        try:
            validate_transition(spec.status, "needs_clarification")
        except InvalidTransition as e:
            raise HTTPException(status_code=409, detail=str(e))
        spec.status = "needs_clarification"
        review = _save_and_review(spec)
        if log_manager is not None:
            try:
                log_manager.append(spec.id, f"spec request-changes (api): {body.reason}")
            except Exception:
                pass
        return review

    return app
