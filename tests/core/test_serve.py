from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.spec import AcceptanceCriterion, Spec
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
