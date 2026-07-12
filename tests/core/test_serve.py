from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient

from mship.core.message_store import MessageStore
from mship.core.serve import create_app
from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec
from mship.core.spec_body import render_body
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.log import LogManager
from mship.core.workitem_store import WorkItemStore


def _app(tmp_path: Path):
    state = StateManager(tmp_path / ".mothership")
    return create_app(
        specs_dir=tmp_path / "specs",
        state_manager=state,
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
    )


def _seed_spec(tmp_path: Path):
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id="dq", title="Decision queue", status="needs_review",
        created_at=now, updated_at=now, task_slug="dq",
        body=render_body("the problem", "as a user", "the approach"),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="view questions", verdict="approved")],
        open_questions=[OpenQuestion(id="q1", text="Mobile too?")],
    ))


def test_health(tmp_path):
    client = TestClient(_app(tmp_path))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "workspace": "test-ws"}


def test_list_specs(tmp_path):
    _seed_spec(tmp_path)
    r = TestClient(_app(tmp_path)).get("/specs")
    assert r.status_code == 200
    assert r.json() == [{"id": "dq", "title": "Decision queue", "status": "needs_review", "task_slug": "dq", "affected_repos": []}]


def test_get_spec_and_404(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    assert client.get("/specs/dq").json()["id"] == "dq"
    assert client.get("/specs/nope").status_code == 404


def test_get_review(tmp_path):
    _seed_spec(tmp_path)
    r = TestClient(_app(tmp_path)).get("/specs/dq/review")
    assert r.status_code == 200
    body = r.json()
    assert body["acceptance_criteria"][0]["id"] == "ac1"
    assert body["summary"]["approved"] == 1


def _seed_task(tmp_path: Path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir(exist_ok=True)
    sm = StateManager(state_dir)
    sm.save(WorkspaceState(tasks={"dq": Task(
        slug="dq", description="d", phase="dev",
        created_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
        affected_repos=["mothership"], branch="feat/dq",
    )}))
    log = LogManager(state_dir / "logs")
    log.append("dq", "spawned")
    return sm, log


def _app_with(tmp_path, sm, log):
    return create_app(specs_dir=tmp_path / "specs", state_manager=sm,
                      log_manager=log, workspace_root=tmp_path, workspace_name="test-ws")


def test_list_and_get_task(tmp_path):
    sm, log = _seed_task(tmp_path)
    client = TestClient(_app_with(tmp_path, sm, log))
    assert any(t["slug"] == "dq" for t in client.get("/tasks").json())
    assert client.get("/tasks/dq").json()["slug"] == "dq"
    assert client.get("/tasks/nope").status_code == 404


def test_journal(tmp_path):
    sm, log = _seed_task(tmp_path)
    client = TestClient(_app_with(tmp_path, sm, log))
    entries = client.get("/journal/dq").json()
    assert any("spawned" in e["message"] for e in entries)
    assert client.get("/journal/nope").status_code == 404


def test_post_is_405(tmp_path):
    # No write routes registered → POST to a GET path is 405 (Method Not Allowed).
    r = TestClient(_app(tmp_path)).post("/specs/dq/review")
    assert r.status_code == 405


def test_unknown_path_404(tmp_path):
    assert TestClient(_app(tmp_path)).get("/nope").status_code == 404


def _auth_app(tmp_path: Path, token):
    _seed_spec(tmp_path)
    state = StateManager(tmp_path / ".mothership")
    return create_app(
        specs_dir=tmp_path / "specs", state_manager=state, log_manager=None,
        workspace_root=tmp_path, workspace_name="test-ws", auth_token=token,
    )


def test_auth_required_when_token_set(tmp_path):
    client = TestClient(_auth_app(tmp_path, "secret"))
    assert client.get("/specs").status_code == 401
    assert client.get("/specs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/specs", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_open_when_no_token(tmp_path):
    assert TestClient(_auth_app(tmp_path, None)).get("/specs").status_code == 200


def test_docs_disabled_when_token_set(tmp_path):
    client = TestClient(_auth_app(tmp_path, "secret"))
    # No unauthenticated schema/docs surface when exposed behind auth.
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


def test_docs_available_when_no_token(tmp_path):
    client = TestClient(_auth_app(tmp_path, None))
    assert client.get("/openapi.json").status_code == 200


def test_non_ascii_token_still_401_not_500(tmp_path):
    client = TestClient(_auth_app(tmp_path, "tøken-✓"))
    r = client.get("/specs")          # missing header
    assert r.status_code == 401       # fail-closed, not 500
    # Positive case (correct non-ascii token) omitted: httpx/TestClient encodes
    # header values as ASCII and raises UnicodeEncodeError before the request
    # reaches the server, so we cannot test the success path via TestClient.


def test_post_verdict(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/verdict", json={"criterion_id": "ac1", "verdict": "flagged"})
    assert r.status_code == 200
    assert r.json()["acceptance_criteria"][0]["verdict"] == "flagged"
    assert client.post("/specs/dq/verdict", json={"criterion_id": "ac1", "verdict": "bogus"}).status_code == 400
    assert client.post("/specs/dq/verdict", json={"criterion_id": "nope", "verdict": "approved"}).status_code == 400
    assert client.post("/specs/none/verdict", json={"criterion_id": "ac1", "verdict": "approved"}).status_code == 404


def test_post_question_and_answer(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    add = client.post("/specs/dq/questions", json={"text": "Tablets too?"})
    assert add.status_code == 200
    assert [q["id"] for q in add.json()["open_questions"]] == ["q1", "q2"]
    ans = client.post("/specs/dq/questions/q1/answer", json={"answer": "yes"})
    assert ans.status_code == 200
    assert ans.json()["open_questions"][0]["answer"] == "yes"
    assert client.post("/specs/dq/questions/q99/answer", json={"answer": "x"}).status_code == 400


def test_post_question_unknown_spec_404(tmp_path):
    assert TestClient(_app(tmp_path)).post("/specs/none/questions", json={"text": "x"}).status_code == 404


def test_post_approve_gate_and_success(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    blocked = client.post("/specs/dq/approve", json={})
    assert blocked.status_code == 409          # q1 unanswered
    assert isinstance(blocked.json()["detail"], str)   # uniform string shape
    client.post("/specs/dq/questions/q1/answer", json={"answer": "yes"})
    ok = client.post("/specs/dq/approve", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert client.post("/specs/dq/approve", json={}).status_code == 409           # re-approve illegal


def test_post_approve_bypass(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/approve", json={"bypass_gate": True})
    assert r.status_code == 200 and r.json()["status"] == "approved"


def test_post_request_changes(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/request-changes", json={"reason": "tighten scope"})
    # MOS-240: request-changes -> editable `draft` status carrying the reason.
    assert r.status_code == 200 and r.json()["status"] == "draft"


def test_post_request_changes_persists_reason(tmp_path):
    """MOS-215: the reason must land on the persisted spec, not just the
    review payload — verified by reloading via GET /specs/{id}."""
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/request-changes", json={"reason": "tighten scope"})
    assert r.status_code == 200
    assert r.json()["clarification_reason"] == "tighten scope"
    assert client.get("/specs/dq").json()["clarification_reason"] == "tighten scope"


def test_get_review_includes_clarification_reason(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    client.post("/specs/dq/request-changes", json={"reason": "tighten scope"})
    r = client.get("/specs/dq/review")
    assert r.status_code == 200
    assert r.json()["clarification_reason"] == "tighten scope"


def test_writes_require_auth(tmp_path):
    client = TestClient(_auth_app(tmp_path, "secret"))
    # No Authorization header → 401 even for a write.
    assert client.post("/specs/dq/verdict", json={"criterion_id": "ac1", "verdict": "approved"}).status_code == 401
    # With the token → allowed.
    ok = client.post(
        "/specs/dq/verdict", json={"criterion_id": "ac1", "verdict": "approved"},
        headers={"Authorization": "Bearer secret"},
    )
    assert ok.status_code == 200


# --- B3: capture-write endpoints (create / draft / apply) ---


def test_post_create_spec(tmp_path):
    client = TestClient(_app(tmp_path))
    r = client.post("/specs", json={"title": "Decision Queue", "affected_repos": ["mothership"]})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "decision-queue"     # slugified title
    assert body["status"] == "draft"
    assert body["affected_repos"] == ["mothership"]
    # persisted → shows up in the list
    assert any(s["id"] == "decision-queue" for s in client.get("/specs").json())


def test_post_create_spec_explicit_id(tmp_path):
    client = TestClient(_app(tmp_path))
    r = client.post("/specs", json={"title": "Anything", "id": "custom"})
    assert r.status_code == 200 and r.json()["id"] == "custom"


def test_post_create_spec_collision_409(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post("/specs", json={"title": "Dup"}).status_code == 200
    assert client.post("/specs", json={"title": "Dup"}).status_code == 409


def test_post_create_spec_unslugifiable_title_400(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post("/specs", json={"title": "!!!"}).status_code == 400


def test_post_draft_returns_prompt_no_mutation(tmp_path):
    client = TestClient(_app(tmp_path))
    client.post("/specs", json={"title": "DQ", "id": "dq"})
    r = client.post("/specs/dq/draft", json={"intent": "I want a decision queue"})
    assert r.status_code == 200
    prompt = r.json()["prompt"]
    assert "I want a decision queue" in prompt          # the intent
    assert "acceptance_criteria" in prompt              # the draft JSON shape
    # draft is read-only: status unchanged
    assert client.get("/specs/dq").json()["status"] == "draft"


def test_post_draft_unknown_spec_404(tmp_path):
    assert TestClient(_app(tmp_path)).post("/specs/none/draft", json={"intent": "x"}).status_code == 404


def test_post_apply_advances_to_needs_review(tmp_path):
    client = TestClient(_app(tmp_path))
    client.post("/specs", json={"title": "DQ", "id": "dq"})   # drafting
    r = client.post("/specs/dq/apply", json={"draft": {
        "problem": "P", "user_story": "U", "approach": "A",
        "acceptance_criteria": ["view questions"], "open_questions": ["android?"],
        "affected_repos": ["mothership"],
    }})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "needs_review"
    assert [c["id"] for c in body["acceptance_criteria"]] == ["ac1"]
    assert body["affected_repos"] == ["mothership"]


def test_post_apply_unknown_spec_404(tmp_path):
    r = TestClient(_app(tmp_path)).post(
        "/specs/none/apply", json={"draft": {"problem": "P", "user_story": "U", "approach": "A"}}
    )
    assert r.status_code == 404


def test_post_apply_illegal_transition_409_and_bypass(tmp_path):
    _seed_spec(tmp_path)   # status needs_review already
    client = TestClient(_app(tmp_path))
    draft = {"draft": {"problem": "P", "user_story": "U", "approach": "A"}}
    assert client.post("/specs/dq/apply", json=draft).status_code == 409      # needs_review → needs_review illegal
    ok = client.post("/specs/dq/apply", json={**draft, "bypass_status_gate": True})
    assert ok.status_code == 200 and ok.json()["status"] == "needs_review"


def test_post_apply_revising_clears_clarification_reason(tmp_path):
    """MOS-215/MOS-240: applying a revised draft to a spec that was sent back
    (draft + clarification_reason) must clear the stale reason."""
    client = TestClient(_app(tmp_path))
    client.post("/specs", json={"title": "DQ", "id": "dq"})   # draft
    client.post("/specs/dq/apply", json={"draft": {
        "problem": "P", "user_story": "U", "approach": "A",
        "acceptance_criteria": ["view questions"], "open_questions": [],
    }})   # -> needs_review
    client.post("/specs/dq/request-changes", json={"reason": "tighten scope"})   # -> draft (+ reason)
    assert client.get("/specs/dq").json()["clarification_reason"] == "tighten scope"

    r = client.post("/specs/dq/apply", json={"draft": {
        "problem": "P2", "user_story": "U", "approach": "A",
        "acceptance_criteria": ["view questions"], "open_questions": [],
    }})
    assert r.status_code == 200 and r.json()["status"] == "needs_review"
    assert client.get("/specs/dq").json()["clarification_reason"] is None


def test_post_approve_clears_clarification_reason(tmp_path):
    """MOS-215 (Greptile): approving a spec that still carries a request-changes
    reason clears it — an approved spec has no pending clarification. Seed a
    needs_review spec with a lingering reason (normal flow clears it on apply;
    this guards the approve path too)."""
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id="dq", title="DQ", status="needs_review",
        created_at=now, updated_at=now, clarification_reason="tighten scope",
        body=render_body("P", "U", "A"),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="approved")],
    ))
    client = TestClient(_app(tmp_path))
    assert client.get("/specs/dq").json()["clarification_reason"] == "tighten scope"

    r = client.post("/specs/dq/approve", json={"bypass_gate": True})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert client.get("/specs/dq").json()["clarification_reason"] is None


def test_capture_writes_require_auth(tmp_path):
    client = TestClient(_auth_app(tmp_path, "secret"))
    assert client.post("/specs", json={"title": "X"}).status_code == 401
    ok = client.post("/specs", json={"title": "X"}, headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200


# --- B4: dispatch endpoint + auto-spawn ---

from types import SimpleNamespace


def _seed_approved_spec(tmp_path: Path, spec_id="dq", repos=("mothership",)):
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id=spec_id, title="Decision queue", status="approved",
        created_at=now, updated_at=now, affected_repos=list(repos),
        body=render_body("the problem", "as a user", "the approach"),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="view questions", verdict="approved")],
    ))


class _FakeWorktreeManager:
    """Stands in for WorktreeManager.spawn — registers a task, no real git."""

    def __init__(self, sm):
        self._sm = sm

    def spawn(self, *, description, repos, slug, workspace_root):
        task = Task(
            slug=slug, description=description, phase="plan",
            created_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
            affected_repos=list(repos), branch=f"feat/{slug}",
            worktrees={r: Path(f"/wt/{slug}/{r}") for r in repos},
        )
        self._sm.mutate(lambda s: s.tasks.__setitem__(slug, task))
        return SimpleNamespace(task=task)


def _empty_state(tmp_path: Path) -> StateManager:
    (tmp_path / ".mothership").mkdir(exist_ok=True)
    sm = StateManager(tmp_path / ".mothership")
    sm.save(WorkspaceState(tasks={}))
    return sm


def test_post_dispatch_binds_existing_task(tmp_path):
    sm, log = _seed_task(tmp_path)        # task "dq", affected_repos=["mothership"]
    _seed_approved_spec(tmp_path)         # approved spec "dq"
    client = TestClient(_app_with(tmp_path, sm, log))
    r = client.post("/specs/dq/dispatch")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["spawned"] is False
    assert body["spec"]["status"] == "dispatched"
    assert body["task_slug"] == "dq"
    assert "view questions" in body["handoff"]
    assert sm.load().tasks["dq"].spec_id == "dq"


def test_post_dispatch_auto_spawns(tmp_path):
    sm = _empty_state(tmp_path)
    _seed_approved_spec(tmp_path, spec_id="cap", repos=["shared"])
    app = create_app(
        specs_dir=tmp_path / "specs", state_manager=sm, log_manager=None,
        workspace_root=tmp_path, workspace_name="t",
        worktree_manager=_FakeWorktreeManager(sm),
    )
    r = TestClient(app).post("/specs/cap/dispatch")
    assert r.status_code == 200, r.text
    assert r.json()["spawned"] is True
    assert sm.load().tasks["cap"].spec_id == "cap"


def test_post_dispatch_not_approved_409(tmp_path):
    _seed_spec(tmp_path)                  # status needs_review
    assert TestClient(_app(tmp_path)).post("/specs/dq/dispatch").status_code == 409


def test_post_dispatch_unknown_spec_404(tmp_path):
    assert TestClient(_app(tmp_path)).post("/specs/none/dispatch").status_code == 404


def test_post_dispatch_auto_spawn_unavailable_409(tmp_path):
    # approved spec, no task, no worktree_manager configured → cannot auto-spawn
    sm = _empty_state(tmp_path)
    _seed_approved_spec(tmp_path, spec_id="cap", repos=["shared"])
    app = create_app(
        specs_dir=tmp_path / "specs", state_manager=sm, log_manager=None,
        workspace_root=tmp_path, workspace_name="t",
    )
    assert TestClient(app).post("/specs/cap/dispatch").status_code == 409


# --- MOS-194: serve dispatch posts an agent-event handoff into the WorkItem thread ---


def _dispatch_app(tmp_path, spec_id="cap", repos=("shared",)):
    sm = _empty_state(tmp_path)
    _seed_approved_spec(tmp_path, spec_id=spec_id, repos=list(repos))
    app = create_app(
        specs_dir=tmp_path / "specs", state_manager=sm, log_manager=None,
        workspace_root=tmp_path, workspace_name="t",
        worktree_manager=_FakeWorktreeManager(sm),
    )
    return app, sm


def test_post_dispatch_posts_agent_event_handoff(tmp_path):
    app, sm = _dispatch_app(tmp_path)
    r = TestClient(app).post("/specs/cap/dispatch")
    assert r.status_code == 200, r.text
    wi_id = r.json()["spec"]["work_item_id"]
    assert wi_id

    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    msgs = MessageStore(tmp_path / ".mothership" / "messages")
    wi = items.get(wi_id)
    assert wi is not None and wi.thread_ids, "dispatch should create+link a thread for the handoff event"
    thread = msgs.get(wi.thread_ids[0])
    assert thread is not None

    # A seed (human) message plus exactly one agent event carrying the handoff.
    assert [m.role for m in thread.messages] == ["human", "agent"]
    event = thread.messages[-1]
    assert event.kind == "event"
    assert "dispatch cap -> cap" in event.text     # stable marker (spec id -> task slug)
    assert "cap" in event.text                     # spec id / task slug
    assert "/wt/cap/shared" in event.text          # worktree path, from the rendered handoff

    assert thread.awaiting_agent_event is True
    assert thread.needs_you is False


def test_post_dispatch_idempotent_no_double_event(tmp_path):
    app, sm = _dispatch_app(tmp_path)
    client = TestClient(app)
    first = client.post("/specs/cap/dispatch")
    assert first.status_code == 200, first.text
    wi_id = first.json()["spec"]["work_item_id"]

    second = client.post("/specs/cap/dispatch")   # re-dispatch: idempotent bind, no new task
    assert second.status_code == 200, second.text
    assert second.json()["spawned"] is False

    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    msgs = MessageStore(tmp_path / ".mothership" / "messages")
    wi = items.get(wi_id)
    thread = msgs.get(wi.thread_ids[0])
    events = [m for m in thread.messages if m.kind == "event"]
    assert len(events) == 1                        # no double-post on re-dispatch


def test_post_dispatch_never_500s_if_notify_raises(tmp_path, monkeypatch):
    sm, log = _seed_task(tmp_path)                 # task "dq", affected_repos=["mothership"]
    _seed_approved_spec(tmp_path)                  # approved spec "dq"
    monkeypatch.setattr(
        "mship.core.serve._notify_dispatch",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("mailbox glitch")),
    )
    client = TestClient(_app_with(tmp_path, sm, log))
    r = client.post("/specs/dq/dispatch")
    assert r.status_code == 200, r.text            # dispatch itself is unaffected
    assert r.json()["spec"]["status"] == "dispatched"


# --- message mailbox endpoints ---


def test_threads_create_append_list_get(tmp_path):
    client = TestClient(_app(tmp_path))

    # create a thread (derives subject from text when omitted)
    r = client.post("/threads", json={"text": "build a thing that does X"})
    assert r.status_code == 200
    thread = r.json()
    tid = thread["id"]
    assert thread["subject"].startswith("build a thing")
    assert [m["role"] for m in thread["messages"]] == ["human"]
    assert thread["awaiting_reply"] is True   # computed_field serialized into the response

    # list shows it, awaiting an agent
    lst = client.get("/threads").json()
    assert any(t["id"] == tid and t["awaiting_reply"] is True for t in lst)

    # append a human message
    r2 = client.post(f"/threads/{tid}/messages", json={"text": "second thought"})
    assert r2.status_code == 200
    assert len(r2.json()["messages"]) == 2

    # get full thread
    full = client.get(f"/threads/{tid}").json()
    assert [m["text"] for m in full["messages"]] == ["build a thing that does X", "second thought"]
    assert full["awaiting_reply"] is True


def test_threads_404s(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.get("/threads/nope").status_code == 404
    assert client.post("/threads/nope/messages", json={"text": "x"}).status_code == 404


def test_threads_explicit_subject(tmp_path):
    client = TestClient(_app(tmp_path))
    t = client.post("/threads", json={"text": "body", "subject": "My subject"}).json()
    assert t["subject"] == "My subject"


def test_thread_exposes_spec_id(tmp_path):
    from mship.core.message_store import MessageStore
    client = TestClient(_app(tmp_path))
    tid = client.post("/threads", json={"text": "hi"}).json()["id"]
    MessageStore(tmp_path / ".mothership" / "messages").link_spec(tid, "spec-1")
    assert client.get(f"/threads/{tid}").json()["spec_id"] == "spec-1"


def test_thread_detail_exposes_related_work_item(tmp_path):
    from mship.core.message_store import MessageStore
    from mship.core.workitem_store import WorkItemStore

    _seed_spec(tmp_path)  # spec id "dq", status "needs_review" -> phase "shaping"
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Decision queue", kind="feature", workspace="test-ws", now=now)
    items.link_spec(wi.id, "dq", now=now)

    client = TestClient(_app(tmp_path))
    tid = client.post("/threads", json={"text": "hi"}).json()["id"]
    MessageStore(tmp_path / ".mothership" / "messages").link_spec(tid, "dq")

    body = client.get(f"/threads/{tid}").json()
    assert body["work_item_id"] == wi.id
    assert body["work_item"] == {
        "id": wi.id, "title": "Decision queue", "kind": "feature", "phase": "shaping",
    }


def test_thread_detail_null_work_item_when_unrelated(tmp_path):
    from mship.core.workitem_store import WorkItemStore

    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    items.create(title="Unrelated", kind="feature", workspace="test-ws", now=now)

    client = TestClient(_app(tmp_path))
    tid = client.post("/threads", json={"text": "hi"}).json()["id"]

    body = client.get(f"/threads/{tid}").json()
    assert body["work_item_id"] is None
    assert body["work_item"] is None


def test_thread_detail_resolves_work_item_id_when_archived(tmp_path):
    """MOS-228 fix: archiving a WorkItem must not break the thread->work-item link
    graph. GET /threads/{id} still needs to report work_item_id for a thread whose
    owning item is archived — only the user-facing GET /items listing should drop
    it (regression test for the link-resolution using the archived-excluding
    workitems.list() default)."""
    from mship.core.workitem_store import WorkItemStore

    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Old work", kind="feature", workspace="test-ws", now=now)

    client = TestClient(_app(tmp_path))
    tid = client.post("/threads", json={"text": "hi"}).json()["id"]
    items.add_thread(wi.id, tid, now=now)

    items.archive(wi.id, now=now)

    body = client.get(f"/threads/{tid}").json()
    assert body["work_item_id"] == wi.id
    assert body["work_item"] == {
        "id": wi.id, "title": "Old work", "kind": "feature", "phase": "inbox",
    }

    # The user-facing listing still excludes the archived item.
    assert client.get("/items").json() == []


def test_thread_detail_linkifies_spec_ref_in_agent_message_only(tmp_path):
    from mship.core.message_store import MessageStore
    from datetime import datetime, timezone, timedelta

    _seed_spec(tmp_path)  # spec id "dq"
    client = TestClient(_app(tmp_path))
    tid = client.post("/threads", json={"text": "hi"}).json()["id"]

    store = MessageStore(tmp_path / ".mothership" / "messages")
    base = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    store.append(tid, "agent", "see spec dq for details", base)
    store.append(tid, "human", "spec dq mentioned again", base + timedelta(minutes=1))

    messages = client.get(f"/threads/{tid}").json()["messages"]
    agent_msg = next(m for m in messages if m["role"] == "agent")
    human_msg = next(m for m in messages if m["text"].startswith("spec dq mentioned"))
    assert agent_msg["text"] == "see spec [dq](groundcontrol://spec?id=dq) for details"
    assert human_msg["text"] == "spec dq mentioned again"


def test_thread_summaries_expose_needs_you_and_unseen(tmp_path):
    from mship.core.message_store import MessageStore
    from datetime import datetime, timezone, timedelta
    store = MessageStore(tmp_path / ".mothership" / "messages")
    base = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    t = store.create_thread("s", "hi", base)
    store.append(t.id, "agent", "need you", base + timedelta(minutes=1), kind="needs_you")

    client = TestClient(_app(tmp_path))
    summary = next(x for x in client.get("/threads").json() if x["id"] == t.id)
    assert summary["needs_you"] is True
    assert summary["unseen"] is True
    assert summary["awaiting_reply"] is False


def test_thread_summaries_expose_needs_decision(tmp_path):
    from mship.core.message_store import MessageStore
    from mship.core.message import DecisionPayload
    from datetime import datetime, timezone, timedelta
    store = MessageStore(tmp_path / ".mothership" / "messages")
    base = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    t = store.create_thread("s", "hi", base)
    store.append(
        t.id, "agent", "pick one", base + timedelta(minutes=1),
        kind="decision", decision=DecisionPayload(options=["a", "b"]),
    )

    client = TestClient(_app(tmp_path))
    summary = next(x for x in client.get("/threads").json() if x["id"] == t.id)
    assert summary["needs_decision"] is True


def test_post_seen_marks_thread_and_clears_unseen(tmp_path):
    from mship.core.message_store import MessageStore
    from datetime import datetime, timezone, timedelta
    store = MessageStore(tmp_path / ".mothership" / "messages")
    base = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    t = store.create_thread("s", "hi", base)
    store.append(t.id, "agent", "fyi", base + timedelta(minutes=1))

    client = TestClient(_app(tmp_path))
    assert next(x for x in client.get("/threads").json() if x["id"] == t.id)["unseen"] is True
    r = client.post(f"/threads/{t.id}/seen", json={"seen_at": (base + timedelta(minutes=2)).isoformat()})
    assert r.status_code == 200
    assert next(x for x in client.get("/threads").json() if x["id"] == t.id)["unseen"] is False


def test_post_seen_unknown_thread_404(tmp_path):
    client = TestClient(_app(tmp_path))
    r = client.post("/threads/nope/seen", json={"seen_at": "2026-06-30T12:00:00+00:00"})
    assert r.status_code == 404


def test_post_seen_defaults_to_now_when_omitted(tmp_path):
    from mship.core.message_store import MessageStore
    from datetime import datetime, timezone, timedelta
    store = MessageStore(tmp_path / ".mothership" / "messages")
    base = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    t = store.create_thread("s", "hi", base)
    store.append(t.id, "agent", "fyi", base + timedelta(minutes=1))
    client = TestClient(_app(tmp_path))
    r = client.post(f"/threads/{t.id}/seen", json={})
    assert r.status_code == 200
    assert next(x for x in client.get("/threads").json() if x["id"] == t.id)["unseen"] is False


def test_post_seen_malformed_value_returns_422(tmp_path):
    from mship.core.message_store import MessageStore
    from datetime import datetime, timezone
    store = MessageStore(tmp_path / ".mothership" / "messages")
    t = store.create_thread("s", "hi", datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc))
    client = TestClient(_app(tmp_path))
    # a non-empty but unparseable timestamp -> 422
    assert client.post(f"/threads/{t.id}/seen", json={"seen_at": "not-a-date"}).status_code == 422
    # an empty string is malformed too (distinct from an omitted seen_at) -> 422
    assert client.post(f"/threads/{t.id}/seen", json={"seen_at": ""}).status_code == 422


def test_post_item_unattended_toggles_flag(tmp_path):
    from mship.core.workitem_store import WorkItemStore
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="t", kind="feature", workspace="test-ws",
                      now=datetime(2026, 7, 8, tzinfo=timezone.utc))
    assert wi.unattended is False
    client = TestClient(_app(tmp_path))

    r = client.post(f"/items/{wi.id}/unattended", json={"on": True})
    assert r.status_code == 200
    assert r.json() == {"id": wi.id, "unattended": True}
    assert items.get(wi.id).unattended is True

    r = client.post(f"/items/{wi.id}/unattended", json={"on": False})
    assert r.status_code == 200
    assert r.json() == {"id": wi.id, "unattended": False}
    assert items.get(wi.id).unattended is False


def test_post_item_unattended_404_for_unknown_item(tmp_path):
    client = TestClient(_app(tmp_path))
    r = client.post("/items/nope/unattended", json={"on": True})
    assert r.status_code == 404


# --- gc32 ac4: POST /specs/{id}/archive (swipe-to-archive) ---

def _seed_status_spec(tmp_path: Path, status: str, spec_id="ar"):
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id=spec_id, title="Archive me", status=status,
        created_at=now, updated_at=now,
    ))


def test_post_archive_from_implemented(tmp_path):
    _seed_status_spec(tmp_path, "implemented")
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/ar/archive")
    assert r.status_code == 200
    # Finding 4: archive returns the same fuller review payload as approve/apply (not
    # just {id,status}) so a client cache isn't degraded on the round-trip.
    body = r.json()
    assert body["id"] == "ar" and body["status"] == "archived"
    assert "acceptance_criteria" in body and "summary" in body and "context" in body
    assert SpecStore(tmp_path / "specs").find_by_id("ar").status == "archived"


def test_post_archive_from_any_non_terminal_state(tmp_path):
    # Decluttering: archive is reachable from any non-terminal status, not only
    # implemented -> archived.
    for i, status in enumerate(
        ["draft", "needs_review", "approved", "dispatched"]
    ):
        sid = f"s{i}"
        _seed_status_spec(tmp_path, status, spec_id=sid)
        client = TestClient(_app(tmp_path))
        r = client.post(f"/specs/{sid}/archive")
        assert r.status_code == 200, (status, r.text)
        body = r.json()
        assert body["id"] == sid and body["status"] == "archived"
        assert "acceptance_criteria" in body and "summary" in body


def test_post_archive_unknown_spec_404(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post("/specs/nope/archive").status_code == 404


def test_post_archive_already_archived_409(tmp_path):
    _seed_status_spec(tmp_path, "archived")
    client = TestClient(_app(tmp_path))
    assert client.post("/specs/ar/archive").status_code == 409
