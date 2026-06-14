from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec
from mship.core.spec_body import render_body
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.log import LogManager


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
    assert r.json() == [{"id": "dq", "title": "Decision queue", "status": "needs_review", "task_slug": "dq"}]


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
    assert r.status_code == 200 and r.json()["status"] == "needs_clarification"


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
    assert body["status"] == "drafting"
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
    assert client.get("/specs/dq").json()["status"] == "drafting"


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
