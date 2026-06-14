from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.spec import AcceptanceCriterion, Spec
from mship.core.spec_body import render_body
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager


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
