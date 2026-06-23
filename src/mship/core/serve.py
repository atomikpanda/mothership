from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from mship.core.spec import SpecDraft


class VerdictBody(BaseModel):
    criterion_id: str
    verdict: str


class NewSpecBody(BaseModel):
    title: str
    id: str | None = None
    affected_repos: list[str] = []
    task_slug: str | None = None


class DraftIntentBody(BaseModel):
    intent: str


class ApplyDraftBody(BaseModel):
    draft: SpecDraft
    bypass_status_gate: bool = False


class QuestionBody(BaseModel):
    text: str


class AnswerBody(BaseModel):
    answer: str


class ApproveBody(BaseModel):
    bypass_gate: bool = False


class ReasonBody(BaseModel):
    reason: str


class NewThreadBody(BaseModel):
    text: str
    subject: str | None = None


class NewMessageBody(BaseModel):
    text: str


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
    worktree_manager=None,
):
    """Build the mship serve FastAPI app (read + review/approve write endpoints).
    Sync handlers call the core directly; FastAPI serializes the returns.

    `worktree_manager` (optional) enables the dispatch endpoint to auto-spawn a
    task when none exists; without it, dispatch can only bind a pre-existing task."""
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
            {"id": s.id, "title": s.title, "status": s.status, "task_slug": s.task_slug, "affected_repos": s.affected_repos}
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
                raise HTTPException(status_code=409, detail="cannot approve: " + "; ".join(blockers))
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

    # --- capture-write endpoints (B3): the phone Capture path over HTTP ---

    from mship.core.spec_draft import apply_draft, build_draft_prompt, new_spec

    @app.post("/specs")
    def post_create_spec(body: NewSpecBody):
        now = datetime.now(timezone.utc)
        try:
            spec = new_spec(
                body.title, now=now, spec_id=body.id,
                affected_repos=body.affected_repos, task_slug=body.task_slug,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if store.find_by_id(spec.id) is not None:
            raise HTTPException(status_code=409, detail=f"spec {spec.id!r} already exists")
        store.save(spec)
        return spec.model_dump(mode="json")

    @app.post("/specs/{spec_id}/draft")
    def post_draft(spec_id: str, body: DraftIntentBody):
        _load_or_404(spec_id)
        return {"prompt": build_draft_prompt(spec_id, body.intent)}

    @app.post("/specs/{spec_id}/apply")
    def post_apply(spec_id: str, body: ApplyDraftBody):
        spec = _load_or_404(spec_id)
        if not body.bypass_status_gate:
            try:
                validate_transition(spec.status, "needs_review")
            except InvalidTransition as e:
                raise HTTPException(status_code=409, detail=str(e))
        apply_draft(spec, body.draft)
        spec.status = "needs_review"
        spec.updated_at = datetime.now(timezone.utc)
        store.save(spec)
        return spec.model_dump(mode="json")

    # --- dispatch endpoint (B4): close the dispatch-from-phone loop ---

    from mship.core.spec_dispatch import DispatchError, dispatch_spec

    def _serve_spawn(s):
        if worktree_manager is None:
            raise DispatchError(
                "auto-spawn unavailable: this server has no worktree manager; "
                f"spawn a task named {s.id!r} first, then dispatch."
            )
        return worktree_manager.spawn(
            description=s.title, repos=list(s.affected_repos),
            slug=s.id, workspace_root=workspace_root,
        ).task

    @app.post("/specs/{spec_id}/dispatch")
    def post_dispatch(spec_id: str):
        spec = _load_or_404(spec_id)
        try:
            result = dispatch_spec(
                spec, state_manager=state_manager, store=store,
                spawn_fn=_serve_spawn, now=datetime.now(timezone.utc),
            )
        except DispatchError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {
            "spec": result.spec.model_dump(mode="json"),
            "task_slug": result.task.slug,
            "spawned": result.spawned,
            "handoff": result.handoff,
        }

    # --- message mailbox (phone <-> agent) ---
    from mship.core.message_store import MessageStore

    msgs = MessageStore(workspace_root / ".mothership" / "messages")

    @app.post("/threads")
    def post_thread(body: NewThreadBody):
        now = datetime.now(timezone.utc)
        text = body.text
        subject = body.subject or (text.strip().splitlines()[0][:80] if text.strip() else "(no subject)")
        return msgs.create_thread(subject=subject, text=text, now=now).model_dump(mode="json")

    @app.post("/threads/{thread_id}/messages")
    def post_message(thread_id: str, body: NewMessageBody):
        now = datetime.now(timezone.utc)
        try:
            msgs.append(thread_id, "human", body.text, now)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id!r}")
        return msgs.get(thread_id).model_dump(mode="json")

    @app.get("/threads")
    def list_threads():
        return [
            {
                "id": t.id, "subject": t.subject,
                "updated_at": t.updated_at.isoformat(),
                "awaiting_reply": t.awaiting_reply,
                "last_message": (t.messages[-1].text[:120] if t.messages else ""),
                "message_count": len(t.messages),
            }
            for t in msgs.list()
        ]

    @app.get("/threads/{thread_id}")
    def get_thread(thread_id: str):
        t = msgs.get(thread_id)
        if t is None:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id!r}")
        return t.model_dump(mode="json")

    return app
