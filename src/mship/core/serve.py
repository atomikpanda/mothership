from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from mship.core.pr import PRManager
from mship.core.pr_watcher import PrWatcher
from mship.core.spec import SpecDraft
from mship.core.workitem import Phase
from mship.util.shell import ShellRunner

logger = logging.getLogger(__name__)

# Default interval (seconds) between `PrWatcher` sweeps in `mship serve`'s
# background loop (see `_lifespan` in `create_app`). Overridable via
# MSHIP_PR_WATCH_INTERVAL so tests can shrink it (fast, deterministic) or
# disable the loop entirely (<= 0 => no watcher task is created at all).
PR_WATCH_INTERVAL_SECONDS = 45


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


class SeenBody(BaseModel):
    seen_at: str | None = None


class UnattendedBody(BaseModel):
    on: bool = True


class PhaseOverrideBody(BaseModel):
    # `null` clears the override (Reopen -> derived phase); a Phase value pins it
    # (Mark done -> "done"). The Phase Literal gives 422 on an unknown phase.
    phase: Phase | None = None


def _make_auth_dependency(token: str):
    import hmac
    from fastapi import Header, HTTPException

    expected = f"Bearer {token}".encode("utf-8")

    def _require_token(authorization: str | None = Header(default=None)):
        provided = (authorization or "").encode("utf-8")
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    return _require_token


async def _pr_watch_loop(watcher: PrWatcher, stop: asyncio.Event, interval: float) -> None:
    """Runs `watcher.check_once()` off the event loop (it shells out to `gh`)
    every `interval` seconds until `stop` is set. The first sweep happens
    immediately on entry rather than after the first interval, and the loop
    wakes promptly (not after a full interval) once `stop` is set, since it
    waits on `stop.wait()` itself rather than sleeping blindly.

    A failed sweep is logged and swallowed rather than killing the loop —
    `PrWatcher.check_once` already isolates failures per-PR, but this is a
    second, coarser layer of defense in case something outside that (e.g.
    `state_manager.load()`) raises."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(watcher.check_once)
        except Exception:
            logger.exception("pr-watch tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


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

    store = SpecStore(specs_dir)
    pr_manager = PRManager(ShellRunner())

    @asynccontextmanager
    async def _lifespan(_app):
        """Runs a `PrWatcher` sweep on an interval for the app's lifetime,
        started on ASGI startup and cancelled cleanly on shutdown.

        `msgs`, `workitems`, and `_item_msg_lock` are defined further down in
        `create_app`'s body (after this closure). That's safe: this coroutine
        body only runs once uvicorn/TestClient drives the lifespan — always
        after `create_app` has returned and therefore already finished
        assigning them. Attached to both `FastAPI(...)` constructors below."""
        interval = float(os.environ.get("MSHIP_PR_WATCH_INTERVAL", PR_WATCH_INTERVAL_SECONDS))
        if interval <= 0:
            yield
            return
        watcher = PrWatcher(
            msgs, workitems, state_manager,
            check_state=lambda u: pr_manager.check_pr_state(u).state,
            now_fn=lambda: datetime.now(timezone.utc),
            lock=_item_msg_lock,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(_pr_watch_loop(watcher, stop, interval))
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    if auth_token:
        dependencies = [Depends(_make_auth_dependency(auth_token))]
        # Auth covers user routes but NOT FastAPI's built-in docs/openapi routes,
        # so disable them when exposed behind auth (no unauthenticated schema surface).
        app = FastAPI(
            title="mship serve", version="0", dependencies=dependencies,
            docs_url=None, redoc_url=None, openapi_url=None,
            lifespan=_lifespan,
        )
    else:
        app = FastAPI(title="mship serve", version="0", lifespan=_lifespan)

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

    @app.post("/specs/{spec_id}/archive")
    def post_archive(spec_id: str):
        """Archive a spec (swipe-to-archive, gc32 ac4). Reachable from any
        non-terminal status via the `can_transition` abandon rule; re-archiving an
        already-archived spec is rejected (409).

        Finding 4: returns the same fuller review payload as approve/request-changes
        (via `_save_and_review`) rather than a bare `{id,status}`, so a client cache
        isn't degraded on the round-trip."""
        spec = _load_or_404(spec_id)
        try:
            validate_transition(spec.status, "archived")
        except InvalidTransition as e:
            raise HTTPException(status_code=409, detail=str(e))
        spec.status = "archived"
        return _save_and_review(spec)

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
        # Serialized end-to-end (_dispatch_lock): two concurrent dispatches of the
        # same spec both run in Starlette's sync threadpool, and when
        # spec.work_item_id is still None, dispatch_spec's create-or-reuse branch
        # can't tell the second caller apart from the first until store.save(spec)
        # lands. Re-loading the spec inside the lock (rather than reusing the copy
        # loaded before it) means the second caller sees the first's work_item_id
        # already set and reuses it instead of creating a duplicate WorkItem.
        try:
            with _dispatch_lock:
                spec = _load_or_404(spec_id)
                result = dispatch_spec(
                    spec, state_manager=state_manager, store=store,
                    spawn_fn=_serve_spawn, now=datetime.now(timezone.utc),
                    workitems=workitems, workspace=workspace_name,
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
        return _thread_payload(msgs.create_thread(subject=subject, text=text, now=now))

    @app.post("/threads/{thread_id}/messages")
    def post_message(thread_id: str, body: NewMessageBody):
        now = datetime.now(timezone.utc)
        try:
            msgs.append(thread_id, "human", body.text, now)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id!r}")
        t = msgs.get(thread_id)
        if t is None:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id!r}")
        return _thread_payload(t)

    @app.post("/threads/{thread_id}/seen")
    def post_seen(thread_id: str, body: SeenBody):
        # `is not None` (not truthiness): an empty string is a malformed timestamp
        # (-> 422 below), distinct from an omitted seen_at (None -> "now").
        if body.seen_at is not None:
            try:
                seen_dt = datetime.fromisoformat(body.seen_at)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"invalid seen_at: {body.seen_at!r}")
            if seen_dt.tzinfo is None:
                seen_dt = seen_dt.replace(tzinfo=timezone.utc)
        else:
            seen_dt = datetime.now(timezone.utc)
        try:
            t = msgs.mark_seen(thread_id, seen_dt)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id!r}")
        return _thread_payload(t)

    def _summaries(threads):
        return [
            {
                "id": t.id, "subject": t.subject,
                "updated_at": t.updated_at.isoformat(),
                "awaiting_reply": t.awaiting_reply,
                "needs_you": t.needs_you,
                "needs_decision": t.needs_decision,
                "unseen": t.unseen,
                "last_message": (t.messages[-1].text[:120] if t.messages else ""),
                "message_count": len(t.messages),
            }
            for t in threads
        ]

    @app.get("/threads")
    async def list_threads(wait: int = 0, since: Optional[str] = None, timeout: float = 25.0):
        if not wait:
            return _summaries(msgs.list())
        from mship.core.message_wait import changed_since
        timeout = max(0.0, min(timeout, 30.0))  # cap for the relay idle-read timeout
        try:
            since_dt = datetime.fromisoformat(since) if since else datetime.now(timezone.utc)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid since value: {since!r}")
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
        interval = 1.0
        deadline = _time.monotonic() + timeout
        while True:
            changed, cursor = changed_since(msgs.list(), since_dt)
            if changed:
                return {"threads": _summaries(changed), "cursor": cursor.isoformat(), "timed_out": False}
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return {"threads": [], "cursor": cursor.isoformat(), "timed_out": True}
            await asyncio.sleep(min(interval, remaining))

    @app.get("/threads/{thread_id}")
    def get_thread(thread_id: str):
        t = msgs.get(thread_id)
        if t is None:
            raise HTTPException(status_code=404, detail=f"no thread {thread_id!r}")
        return _thread_payload(t)

    # --- work items (phase-aware cockpit spine) ---
    from mship.core.workitem_store import WorkItemStore
    from mship.core.view.workitem_index import build_workitem_index
    from mship.core.view.thread_links import resolve_thread_work_item
    from mship.core.view.entity_links import linkify_entities

    workitems = WorkItemStore(workspace_root / ".mothership" / "workitems")
    # Serializes the lazy read-decide-create-link in POST /items/{id}/messages. Sync
    # endpoints run in Starlette's threadpool, so two concurrent first-steers on a
    # threadless item could otherwise both create a thread (orphaning one message) or
    # lose the add_thread update. threading.Lock is the right primitive for that pool.
    _item_msg_lock = threading.Lock()
    # Serializes POST /specs/{id}/dispatch. Same threadpool hazard as above: two
    # concurrent dispatches of the same spec (spec.work_item_id still None) could
    # otherwise both take dispatch_spec's create branch before either store.save(spec)
    # lands, producing a duplicate/orphaned WorkItem. See post_dispatch for the re-load.
    _dispatch_lock = threading.Lock()

    def _workitem_index():
        return build_workitem_index(
            workitems.list(),
            {s.id: s for s in store.list()},
            dict(state_manager.load().tasks),
            {t.id: t for t in msgs.list()},
        )

    def _summarize_item(item_id: str):
        """Summarize a single work item by id via per-id child lookups (no full
        list() scans), mirroring the direct-get short-circuit of GET /threads/{id},
        /specs/{id}. Returns None if the item does not exist."""
        wi = workitems.get(item_id)
        if wi is None:
            return None
        spec = store.find_by_id(wi.spec_id) if wi.spec_id else None
        tasks = state_manager.load().tasks
        return build_workitem_index(
            [wi],
            {spec.id: spec} if spec else {},
            {s: tasks[s] for s in wi.task_slugs if s in tasks},
            {tid: t for tid in wi.thread_ids if (t := msgs.get(tid))},
        )[0]

    def _thread_payload(t):
        """Enrich a thread's dumped dict with the WorkItem it's related to (read-time
        inversion of the WorkItem link graph — see thread_links.resolve_thread_work_item)
        and auto-linkify native entity refs (wi-/spec/task ids) in agent message text
        (see entity_links.linkify_entities). Human messages are left untouched — the
        linkifier only rewrites text the agent produced. Shared by every handler that
        returns a full thread: GET /threads/{id}, POST /threads, POST /threads/{id}/messages,
        POST /threads/{id}/seen. The GET /threads list/summary endpoint is unaffected."""
        data = t.model_dump(mode="json")
        all_items = list(workitems.list())  # single store scan, reused below
        wi_id = resolve_thread_work_item(t.id, t.spec_id, t.task_slug, all_items)
        data["work_item_id"] = wi_id
        if wi_id is None:
            data["work_item"] = None
        else:
            summ = _summarize_item(wi_id)
            data["work_item"] = None if summ is None else {
                "id": summ.id, "title": summ.title, "kind": summ.kind, "phase": summ.phase,
            }
        item_ids = {w.id for w in all_items}
        spec_ids = {s.id for s in store.list()}
        task_slugs = set(state_manager.load().tasks.keys())
        for msg in data.get("messages", []):
            if msg.get("role") == "agent" and msg.get("text"):
                msg["text"] = linkify_entities(msg["text"], item_ids, spec_ids, task_slugs)
        return data

    @app.get("/items")
    def list_items():
        return jsonable_encoder(_workitem_index())

    @app.get("/items/{item_id}")
    def get_item(item_id: str):
        summary = _summarize_item(item_id)
        if summary is None:
            raise HTTPException(status_code=404, detail=f"no work item {item_id!r}")
        return jsonable_encoder(summary)

    @app.post("/items/{item_id}/messages")
    def post_item_message(item_id: str, body: NewMessageBody):
        """Steer a work item: append a human message to its conversation thread,
        lazily creating+linking a thread the first time. In-flight items created
        from specs/tasks have no thread yet, so posting to POST /threads/{id} has
        nothing to target — the phone would silently drop the message. Item id is
        always present, so this is the send path the console uses. Returns the
        thread, mirroring POST /threads/{thread_id}/messages. The read-decide-create
        section is serialized (_item_msg_lock) so concurrent first-steers can't each
        create a thread and orphan a message."""
        now = datetime.now(timezone.utc)
        with _item_msg_lock:
            wi = workitems.get(item_id)
            if wi is None:
                raise HTTPException(status_code=404, detail=f"no work item {item_id!r}")
            tid = wi.thread_ids[0] if wi.thread_ids else None
            if tid is None:
                subject = wi.title.strip() or (
                    body.text.strip().splitlines()[0][:80] if body.text.strip() else "(no subject)"
                )
                task_slug = wi.task_slugs[0] if wi.task_slugs else None
                thread = msgs.create_thread(subject=subject, text=body.text, now=now, task_slug=task_slug)
                workitems.add_thread(item_id, thread.id, now=now)
                return thread.model_dump(mode="json")
            try:
                msgs.append(tid, "human", body.text, now)
            except KeyError:
                raise HTTPException(status_code=404, detail=f"no thread {tid!r}")
            t = msgs.get(tid)
            if t is None:
                raise HTTPException(status_code=404, detail=f"no thread {tid!r}")
            return t.model_dump(mode="json")

    @app.post("/items/{item_id}/unattended")
    def post_item_unattended(item_id: str, body: UnattendedBody):
        """Toggle a work item's eligibility for unattended (cloud-runner) execution.
        Shares _item_msg_lock with POST /items/{id}/messages: both mutate the same
        on-disk WorkItem file, and serializing writes avoids a lost-update race if a
        steer and a toggle land in the same instant."""
        now = datetime.now(timezone.utc)
        with _item_msg_lock:
            try:
                workitems.set_unattended(item_id, body.on, now=now)
            except KeyError:
                raise HTTPException(status_code=404, detail=f"no work item {item_id!r}")
        return {"id": item_id, "unattended": body.on}

    @app.post("/items/{item_id}/phase")
    def post_item_phase(item_id: str, body: PhaseOverrideBody):
        """Set (Mark done) or clear (Reopen) a work item's phase override. `null`
        returns the item to its derived phase. Shares _item_msg_lock with the other
        item writers to avoid a lost update if a steer/toggle lands concurrently."""
        now = datetime.now(timezone.utc)
        with _item_msg_lock:
            try:
                workitems.set_phase_override(item_id, body.phase, now=now)
            except KeyError:
                raise HTTPException(status_code=404, detail=f"no work item {item_id!r}")
        return {"id": item_id, "phase_override": body.phase}

    return app
