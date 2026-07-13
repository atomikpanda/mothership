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
from typing import Any, Optional

from pydantic import BaseModel

from mship.core.gh_app import GhAppError, mint_installation_token, resolve_installation
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


class EvidenceBody(BaseModel):
    criterion_id: str
    ref: str
    kind: str | None = None
    note: str | None = None


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


class CaptureBody(BaseModel):
    idea: str
    title: str | None = None
    idempotency_key: str | None = None


class SeenBody(BaseModel):
    seen_at: str | None = None


class UnattendedBody(BaseModel):
    on: bool = True


class PhaseOverrideBody(BaseModel):
    # `null` clears the override (Reopen -> derived phase); a Phase value pins it
    # (Mark done -> "done"). The Phase Literal gives 422 on an unknown phase.
    phase: Phase | None = None


class ExecBody(BaseModel):
    """POST /exec/{verb} request body — see `mship.core.remote_exec` for the
    full wire contract (how the response streams task output + exit code)."""
    task: str
    repos: list[str]
    platform: str | None = None
    # Only meaningful for verb == "capture"; mirrors `cli/capture.py`'s
    # `--kind` default. Optional so run/build callers can omit it.
    kind: str = "all"


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


def _dispatch_marker(spec_id: str, task_slug: str) -> str:
    """Stable substring embedded in the posted event's text, scanned by
    `_notify_dispatch` for idempotency (see there) — one marker per
    (spec, task) pair rather than per-dispatch-call, so a re-dispatch of the
    same spec/task is recognized as already-notified."""
    return f"dispatch {spec_id} -> {task_slug}"


def _capture_handoff(thread_id: str, idea: str) -> str:
    """Agent `event` body for a phone idea capture. Instructs the draining agent
    to brainstorm the idea into a spec IN THIS THREAD and finish via from-thread.
    The leading marker line makes the handoff greppable/idempotent per thread."""
    return (
        f"capture-brainstorm {thread_id}\n\n"
        "An idea was captured from the phone to brainstorm into a spec. Run the "
        "brainstorming flow in THIS thread: ask the operator clarifying questions "
        f"one at a time with `mship reply {thread_id} \"...\"`, settle "
        "purpose/scope/approach, then produce the spec with "
        f"`mship spec from-thread {thread_id}` → fill the draft JSON → "
        "`mship spec apply <id> --from-json <file>`, and reply here when it's drafted.\n\n"
        f"Idea: {idea}"
    )


def _notify_dispatch(
    *, msgs: Any, workitems: Any, item_msg_lock: Any,
    spec: Any, task: Any, handoff: str, now: datetime,
) -> bool:
    """Post a one-time agent `event` message announcing a serve-side dispatch's
    handoff (spec id, task slug, worktree paths — the same text
    `build_dispatch_handoff` renders) into the spec's WorkItem thread. This is
    what makes `Thread.awaiting_agent_event` true, so an idle host agent armed
    on `mship inbox wait` (whose predicate already includes
    `awaiting_agent_event`) picks the handoff up automatically — no separate
    flag or wake mechanism needed.

    Called ONLY from `post_dispatch` (`POST /specs/{id}/dispatch`) — never from
    `dispatch_spec` itself — so this is serve-only: `mship spec dispatch` (the
    CLI, which calls `dispatch_spec` directly with no message store in hand)
    never posts an event.

    Idempotency mirrors `PrWatcher` (`pr_watcher.py`): before appending, the
    resolved thread's existing messages are scanned for a prior `kind=="event"`
    message containing `_dispatch_marker(spec.id, task.slug)`; if found, this is
    a re-dispatch and nothing is posted. The marker is embedded as the event
    message's leading line so the scan is reliable across both a re-dispatch in
    the same process and a restart (the scan reads persisted messages, not
    in-memory state).

    Thread resolution mirrors `post_item_message` / `PrWatcher._resolve_thread`:
    reuse the WorkItem's first thread if it has one; otherwise create one
    (linking it back to the WorkItem) and seed it with a short human placeholder
    — `create_thread` always seeds a human message, and the event must land as
    a *separate*, trailing agent message for `awaiting_agent_event` to compute
    True.

    Returns whether a new event message was posted (False when idempotency
    skipped it). The caller wraps this in a broad try/except regardless — a
    mailbox glitch here must never turn a successful dispatch into a 500."""
    if spec.work_item_id is None:
        # Shouldn't happen — dispatch_spec always attaches a WorkItem — but
        # there's nowhere to hang a thread without one, so just skip.
        return False

    marker = _dispatch_marker(spec.id, task.slug)
    text = f"{marker}\n\n{handoff}"

    with item_msg_lock:
        wi = workitems.get(spec.work_item_id)
        if wi is None:
            return False

        if wi.thread_ids:
            tid = wi.thread_ids[0]
        else:
            seed = f"Dispatch handoff for {task.slug}"
            thread = msgs.create_thread(
                subject=spec.title or task.slug, text=seed, now=now, task_slug=task.slug,
            )
            workitems.add_thread(wi.id, thread.id, now=now)
            tid = thread.id

        thread = msgs.get(tid)
        if thread is not None and any(
            m.kind == "event" and marker in m.text for m in thread.messages
        ):
            return False  # already notified (this dispatch or a prior process) — skip

        msgs.append(tid, "agent", text, now, kind="event")
        return True


def create_app(
    specs_dir: Path,
    state_manager,
    log_manager,
    workspace_root: Path,
    workspace_name: str = "mothership",
    auth_token: str | None = None,
    worktree_manager=None,
    config=None,
    gh_app_id: str | None = None,
    gh_app_key: str | None = None,
):
    """Build the mship serve FastAPI app (read + review/approve write endpoints).
    Sync handlers call the core directly; FastAPI serializes the returns.

    `worktree_manager` (optional) enables the dispatch endpoint to auto-spawn a
    task when none exists; without it, dispatch can only bind a pre-existing task.

    `config` (optional, this machine's own `WorkspaceConfig`) enables
    `POST /exec/{verb}` (see `mship.core.remote_exec`) — the serve side of
    `mship run/capture/build --remote`. Without it, `/exec/*` is unavailable
    (503) rather than absent, so a caller gets an actionable message instead
    of a bare 404.

    `gh_app_id` / `gh_app_key` (optional) are the GitHub App broker (Broker B)
    credentials — the App's numeric id and its private-key TEXT (already read
    from the .pem by the caller, never a path). When BOTH are present,
    `GET /gh-token` mints short-lived, repo-scoped installation tokens via
    `mship.core.gh_app` (Task 3); when absent, it falls back to proxying this
    host's own `gh auth token` (Broker A). Broker selection is purely by
    presence of these creds. They are captured in this closure and read by
    `get_gh_token`."""
    from fastapi import Depends, FastAPI, HTTPException

    from mship.core.spec_store import SpecStore
    from mship.core.message_store import MessageStore
    from mship.core.workitem_store import WorkItemStore

    store = SpecStore(specs_dir)
    pr_manager = PRManager(ShellRunner())
    # Separate ShellRunner instance for GET /gh-token (Broker A) rather than
    # reaching into pr_manager's private `_shell` — same class, own lifetime,
    # kept simple to construct/replace independently of PRManager.
    gh_token_shell = ShellRunner()

    # `msgs` and `workitems` back both the mailbox/work-item routes below and
    # the `PrWatcher` started by `_lifespan`; defined here (rather than next
    # to the routes that use them) so `_lifespan` doesn't forward-reference
    # them. `_item_msg_lock` serializes the lazy read-decide-create-link in
    # POST /items/{id}/messages: sync endpoints run in Starlette's
    # threadpool, so two concurrent first-steers on a threadless item could
    # otherwise both create a thread (orphaning one message) or lose the
    # add_thread update. threading.Lock is the right primitive for that pool.
    msgs = MessageStore(workspace_root / ".mothership" / "messages")
    workitems = WorkItemStore(workspace_root / ".mothership" / "workitems")
    _item_msg_lock = threading.Lock()

    @asynccontextmanager
    async def _lifespan(_app):
        """Runs a `PrWatcher` sweep on an interval for the app's lifetime,
        started on ASGI startup and cancelled cleanly on shutdown."""
        interval = float(os.environ.get("MSHIP_PR_WATCH_INTERVAL", PR_WATCH_INTERVAL_SECONDS))
        if interval <= 0:
            yield
            return
        watcher = PrWatcher(
            msgs, workitems, state_manager,
            check_state=lambda u: pr_manager.check_pr_state(u).state,
            now_fn=lambda: datetime.now(timezone.utc),
            lock=_item_msg_lock,
            # Lifecycle hooks (MOS-220): `pr.merged`/`pr.closed`. `config` is
            # this machine's own WorkspaceConfig (optional — see create_app's
            # docstring); without it hooks are simply never evaluated.
            config=config,
            workspace_root=workspace_root,
            shell=ShellRunner(),
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

    # --- gh-token: two brokers, one contract ---
    # Both brokers return the same shape {"token", "expires_at", "repositories"}.
    # Broker selection is decided PURELY by whether App creds (gh_app_id AND
    # gh_app_key) are configured — installation coverage is NOT part of the
    # branch condition:
    #
    #   Broker B (App-backed): when both creds are present. Resolves the App
    #   installation from the repos' owner and mints a short-lived token scoped
    #   to exactly the requested repos (mship.core.gh_app). A workspace must be
    #   single-account, so repos spanning >1 owner is a 400, and a repo the App
    #   isn't installed on is a hard 502 — NEVER a silent fall-through to
    #   Broker A (that would swap the caller's identity without them knowing).
    #
    #   Broker A: otherwise, proxy this host's own `gh auth token`. No
    #   independent repo scoping — it hands back whatever `gh` on this host is
    #   authenticated as, so `expires_at` is always None here and `repositories`
    #   is purely an echo of the query, not an enforced scope.

    # Per-owner installation-id cache for the App backend (process lifetime).
    # A workspace is single-account, so this is effectively one lookup; repeated
    # pulls skip the extra GET /repos/{owner}/{repo}/installation round-trip.
    _installation_cache: dict[str, str] = {}

    @app.get("/gh-token")
    def get_gh_token(repos: str | None = None):
        """Inherits the app-wide bearer dependency (see `_make_auth_dependency`
        above) automatically — no separate auth check needed here."""
        repos_list = [r.strip() for r in repos.split(",") if r.strip()] if repos else []

        # --- Broker B: App-backed mint (selected ONLY by App creds present) ---
        if gh_app_id and gh_app_key:
            if not repos_list:
                raise HTTPException(
                    status_code=400,
                    detail="repos query param is required (owner/repo,...) for App minting",
                )
            owners: set[str] = set()
            short_names: list[str] = []
            for full in repos_list:
                if "/" not in full:
                    raise HTTPException(
                        status_code=400,
                        detail=f"repos must be owner/repo for App minting; got {full!r}",
                    )
                owner, name = full.split("/", 1)
                owners.add(owner)
                short_names.append(name)
            if len(owners) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"repos span multiple accounts {sorted(owners)}; a workspace "
                        "must be a single account (one owner)"
                    ),
                )
            owner = owners.pop()
            try:
                installation_id = _installation_cache.get(owner)
                if installation_id is None:
                    installation_id = resolve_installation(
                        app_id=gh_app_id, private_key=gh_app_key,
                        owner=owner, repo=short_names[0],
                    )
                    _installation_cache[owner] = installation_id
                result = mint_installation_token(
                    app_id=gh_app_id, private_key=gh_app_key,
                    installation_id=installation_id, repos=short_names,
                )
            except GhAppError as e:
                # Hard error, never a silent Broker-A fallback (no identity swap).
                raise HTTPException(status_code=502, detail=str(e)) from e
            # Audit the mint: broker + owner + repos + timestamp, never the token.
            logger.info(
                "gh-token minted: broker=App owner=%s repos=%s at=%s",
                owner, short_names, datetime.now(timezone.utc).isoformat(),
            )
            return result

        # --- Broker A: proxy the serve host's own `gh auth token` ---
        result = gh_token_shell.run("gh auth token", cwd=workspace_root)
        token = (result.stdout or "").strip()
        if result.returncode != 0 or not token:
            raise HTTPException(
                status_code=503,
                detail=(
                    "gh auth token unavailable on serve host — run `gh auth login`, "
                    "or set MSHIP_GH_APP_ID/MSHIP_GH_APP_KEY for App-backed minting"
                ),
            )
        # Audit the mint: timestamp + requested repos, never the token value.
        logger.info(
            "gh-token minted: broker=A repos=%s at=%s",
            repos_list or None, datetime.now(timezone.utc).isoformat(),
        )
        return {"token": token, "expires_at": None, "repositories": repos_list or None}

    # --- write endpoints ---

    # datetime/timezone are imported at module top (needed earlier by _lifespan).
    from mship.core.spec_review import (
        infer_evidence_kind, set_criterion_evidence, set_criterion_verdict,
    )
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

    @app.post("/specs/{spec_id}/evidence")
    def post_evidence(spec_id: str, body: EvidenceBody):
        spec = _load_or_404(spec_id)
        kind = body.kind or infer_evidence_kind(body.ref)
        try:
            set_criterion_evidence(spec, body.criterion_id, kind, body.ref, body.note)
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
        spec.clarification_reason = None  # an approved spec carries no pending request-changes reason
        return _save_and_review(spec)

    @app.post("/specs/{spec_id}/request-changes")
    def post_request_changes(spec_id: str, body: ReasonBody):
        spec = _load_or_404(spec_id)
        # MOS-240: request-changes sends the spec back to the editable `draft`
        # status carrying a non-null clarification_reason (the dropped
        # needs_clarification status is now expressed by that field alone).
        try:
            validate_transition(spec.status, "draft")
        except InvalidTransition as e:
            raise HTTPException(status_code=409, detail=str(e))
        spec.status = "draft"
        spec.clarification_reason = body.reason
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
        # MOS-215/MOS-240: applying a (re)drafted spec supersedes any pending
        # request-changes, so clear the reason — a freshly applied draft carries
        # no outstanding clarification ask. (A brand-new draft has none anyway.)
        apply_draft(spec, body.draft)
        spec.status = "needs_review"
        spec.clarification_reason = None
        spec.updated_at = datetime.now(timezone.utc)
        store.save(spec)
        return spec.model_dump(mode="json")

    @app.post("/capture")
    def post_capture(body: CaptureBody):
        """Idea capture → agent-led brainstorm. Seeds a thread with the idea and
        posts an agent `event` handoff so a host/cloud agent drains it (mship
        _drain / inbox wait), brainstorms it in the thread, and produces a spec.
        Serve does NO drafting — it stays LLM-free. An optional idempotency_key
        makes a retried/duplicated capture return the existing thread instead of
        spawning a second brainstorm (AC6)."""
        idea = body.idea.strip()
        if not idea:
            raise HTTPException(status_code=400, detail="idea must not be empty")
        now = datetime.now(timezone.utc)
        key = (body.idempotency_key or "").strip()
        if key:
            marker = f"capture-key {key}"
            for t in msgs.list():
                if any(m.kind == "event" and marker in m.text for m in t.messages):
                    return _thread_payload(t)
        subject = ((body.title or "").strip() or idea.splitlines()[0])[:80]
        thread = msgs.create_thread(subject=subject, text=idea, now=now)
        handoff = _capture_handoff(thread.id, idea)
        if key:
            handoff = f"capture-key {key}\n{handoff}"
        msgs.append(thread.id, "agent", handoff, now, kind="event")
        return _thread_payload(msgs.get(thread.id))

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
                    workspace_root=workspace_root,
                    docs_dir=(config.docs_dir if config is not None else "docs"),
                )
                # Serve-side-only handoff notify (MOS-194): posts an agent `event`
                # message naming the spec/task/worktree into the WorkItem's thread
                # so an idle host agent armed on `mship inbox wait` picks it up.
                # Never lets a mailbox glitch turn a successful dispatch into a
                # 500 — mirrors PrWatcher's never-raise philosophy.
                try:
                    _notify_dispatch(
                        msgs=msgs, workitems=workitems, item_msg_lock=_item_msg_lock,
                        spec=result.spec, task=result.task, handoff=result.handoff,
                        now=datetime.now(timezone.utc),
                    )
                except Exception:
                    logger.exception(
                        "dispatch handoff notify failed (spec=%s task=%s) — "
                        "dispatch itself succeeded", result.spec.id, result.task.slug,
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
    # `msgs` is defined earlier (see comment above `_lifespan`).

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
    # `workitems` and `_item_msg_lock` are defined earlier (see comment above
    # `_lifespan`).
    from mship.core.view.workitem_index import build_workitem_index
    from mship.core.view.thread_links import resolve_thread_work_item
    from mship.core.view.entity_links import linkify_entities

    # Serializes POST /specs/{id}/dispatch. Same threadpool hazard as above: two
    # concurrent dispatches of the same spec (spec.work_item_id still None) could
    # otherwise both take dispatch_spec's create branch before either store.save(spec)
    # lands, producing a duplicate/orphaned WorkItem. See post_dispatch for the re-load.
    _dispatch_lock = threading.Lock()

    def _workitem_index(include_archived: bool = False):
        return build_workitem_index(
            workitems.list(include_archived=include_archived),
            {s.id: s for s in store.list()},
            dict(state_manager.load().tasks),
            {t.id: t for t in msgs.list()},
            include_archived=include_archived,
        )

    def _summarize_item(item_id: str):
        """Summarize a single work item by id via per-id child lookups (no full
        list() scans), mirroring the direct-get short-circuit of GET /threads/{id},
        /specs/{id}. Returns None if the item does not exist. Unlike GET /items, this
        is NOT subject to the archived filter (MOS-228 T3): a direct fetch by id
        always resolves, archived or not — include_archived=True so build_workitem_index
        doesn't drop it."""
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
            include_archived=True,
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
        # include_archived=True: this is link/ownership resolution (which WorkItem does
        # this thread belong to?), not a user-facing listing. A thread stays linked to its
        # WorkItem after that item is archived (MOS-228 T3), so resolving with the default
        # archived-excluding list() would wrongly report work_item_id=null here. The
        # user-facing filter still applies at GET /items (_workitem_index above) and
        # `item list` — only this internal resolution needs the full set.
        all_items = list(workitems.list(include_archived=True))  # single store scan, reused below
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
    def list_items(include_archived: bool = False):
        return jsonable_encoder(_workitem_index(include_archived))

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
        returns the item to its derived phase. The existence check and the
        commit below each take _item_msg_lock briefly (shared with the other
        item writers, to avoid a lost update if a steer/toggle lands
        concurrently) — but the lock is released while the lifecycle hook (a
        subprocess, potentially slow) runs in between. See below for why.

        Lifecycle hooks (MOS-220): setting an explicit phase (not clearing one)
        fires `workitem.phase.<phase>` before the override below commits — the
        one concrete "WorkItem phase-change" call site today (phase is
        otherwise derived on read via compute_phase, not mutated anywhere
        else). A `required: true` hook's failure blocks the override (422) —
        this still holds: the hook runs, and can still raise, strictly before
        `set_phase_override` is ever called below. A non-required failure is
        fail-open — logged, override still proceeds.

        MOS-220 Greptile fix ("Hook Holds Lock"): the hook must NOT run while
        `_item_msg_lock` is held — a slow/timing-out hook would otherwise
        block unrelated item messages, unattended toggles, and other phase
        changes for up to the hook's timeout. So the lock is taken twice: once
        to check the item exists, released for the (possibly slow) hook call,
        then re-taken to persist the override — mirroring PrWatcher's
        `_fire_lifecycle_hook` (do the bookkeeping under the lock, run the
        subprocess outside it).

        The ONLY reason hooks aren't evaluated here is `config is None` — this
        serve instance has no workspace config wired in at all (see
        `create_app`'s docstring), so there is structurally nothing to
        evaluate against. That check is deliberately its own `if`, nested
        below `body.phase is not None`, rather than folded into one combined
        condition — so a config that DOES exist can never be silently
        dropped by an unrelated branch here matching the CLI's own
        `workitem phase` command (mship.cli.workitem), which always has a
        config and always fires hooks when `body.phase is not None`.

        MOS-220 Greptile fix ("Configless Server Bypasses Hooks", #312): a
        missing config used to just log a warning and still apply the phase
        override below — letting a configless serve instance move a work
        item to e.g. `done` without ever running a `required` hook the CLI
        would have enforced. That's a silent policy bypass, not a graceful
        degradation, so this now fails CLOSED: `config is None` raises 503
        and returns without touching `set_phase_override` at all. Clearing an
        override (`body.phase is None`, no phase name to fire a hook for) is
        unaffected and always applies regardless of config."""
        now = datetime.now(timezone.utc)
        with _item_msg_lock:
            if workitems.get(item_id) is None:
                raise HTTPException(status_code=404, detail=f"no work item {item_id!r}")

        if body.phase is not None:
            if config is not None:
                from mship.core.lifecycle_hooks import HookContext, HookRequiredError, run_hooks
                try:
                    run_hooks(
                        f"workitem.phase.{body.phase}", HookContext(workitem_id=item_id),
                        config=config, workspace_root=workspace_root,
                        shell=ShellRunner(), state_manager=state_manager,
                    )
                except HookRequiredError as e:
                    raise HTTPException(status_code=422, detail=str(e))
            else:
                # Fail closed (Greptile, MOS-220 #312): this serve instance has no
                # workspace config, so there is structurally nothing to evaluate
                # `workitem.phase.<phase>` hooks against. Refuse the mutation
                # rather than silently applying a policy-relevant phase change
                # that a config'd instance (or the CLI) would have gated on a
                # required hook.
                logger.warning(
                    "post_item_phase: no workspace config wired into this "
                    "serve instance — refusing workitem.phase.%s for %s "
                    "(cannot evaluate lifecycle hooks)", body.phase, item_id,
                )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "server has no workspace config; cannot evaluate "
                        "lifecycle hooks for this phase change"
                    ),
                )

        with _item_msg_lock:
            try:
                workitems.set_phase_override(item_id, body.phase, now=now)
            except KeyError:
                raise HTTPException(status_code=404, detail=f"no work item {item_id!r}")
        return {"id": item_id, "phase_override": body.phase}

    # --- remote exec (MOS-191): serve side of `mship run/capture/build --remote` ---
    # Runs a go-task verb on THIS machine and streams its output back — see
    # `mship.core.remote_exec` for the full wire contract (line-buffered task
    # output, terminated by a trailing `__MSHIP_EXIT__ <code>` sentinel line).
    # A fresh `ShellRunner` per request (like `gh_token_shell` above) rather than
    # reaching into pr_manager's — same class, its own lifetime.

    import re
    import secrets

    from mship.core import remote_exec
    from fastapi.responses import StreamingResponse
    from starlette.concurrency import iterate_in_threadpool

    # A task name is interpolated into `branch_pattern` and then into git
    # `fetch`/`checkout`/`reset`/`worktree add` run with `shell=True` on the
    # remote — so validate it here, BEFORE any StreamingResponse is built, and
    # reject anything outside this safe charset (fail fast, never reaching the
    # shell). Mirrors the slug charset the rest of mship uses for task names.
    _TASK_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

    @app.post("/exec/{verb}")
    async def post_exec(verb: str, body: ExecBody):
        if verb not in remote_exec.VERBS:
            raise HTTPException(
                status_code=404,
                detail=f"unknown verb {verb!r}; expected one of {remote_exec.VERBS}",
            )
        if config is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "remote workspace not bootstrapped: this serve host has "
                    "no workspace config wired in — bootstrap this machine "
                    "as an mship workspace (mothership.yaml present) and "
                    "restart `mship serve --relay`"
                ),
            )
        if not _TASK_NAME_RE.match(body.task):
            # Never reaches the shell — a shell-metacharacter task name (e.g.
            # "x; rm -rf ~ #") is refused up front, not streamed as data.
            raise HTTPException(status_code=400, detail="invalid task name")
        deps = remote_exec.RemoteExecDeps(
            config=config, shell=ShellRunner(), workspace_root=workspace_root,
        )
        # Per-request anti-spoof nonce (FIX 2): the task can't predict it, and
        # it's returned as a response HEADER (sent before the streamed body, so
        # the task can't inject it) — the client treats a line as a control
        # record only if it carries this exact nonce. Task stdout that happens
        # to print a bare `__MSHIP_EXIT__ 0` is therefore just output, never a
        # forged exit code.
        nonce = secrets.token_hex(8)
        # `run_verb_stream` is a plain sync generator doing blocking subprocess
        # + git I/O; `iterate_in_threadpool` pulls each chunk in the threadpool
        # so it never blocks the event loop (see the module docstring).
        gen = remote_exec.run_verb_stream(
            verb, body.task, body.repos, body.platform,
            kind=body.kind, deps=deps, nonce=nonce,
        )
        return StreamingResponse(
            iterate_in_threadpool(gen),
            media_type="application/octet-stream",
            headers={"X-Mship-Exec-Nonce": nonce},
        )

    return app
