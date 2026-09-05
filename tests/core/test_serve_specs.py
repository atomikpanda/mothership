from fastapi.testclient import TestClient
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread, current_thread

from mship.core.serve import create_app
from mship.core.spec import Spec
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(
        specs_dir=tmp_path / "specs",
        state_manager=StateManager(tmp_path / ".mothership"),
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
    ))


def _seed(tmp_path: Path) -> tuple[Spec, Spec]:
    now = datetime.now(timezone.utc)
    active = Spec(
        id="active", title="Needle design", status="draft",
        created_at=now, updated_at=now,
    )
    archived = Spec(
        id="archived", title="Completed design", status="implemented",
        created_at=now - timedelta(days=8), updated_at=now - timedelta(days=8),
        body="implementation content",
    )
    store = SpecStore(tmp_path / "specs")
    store.save(active)
    store.save(archived)
    return active, archived


def test_specs_filter_search_and_compatibility_default(tmp_path: Path):
    active, archived = _seed(tmp_path)
    client = _client(tmp_path)

    all_specs = client.get("/specs")
    assert all_specs.status_code == 200
    assert {spec["id"] for spec in all_specs.json()} == {active.id, archived.id}
    assert {spec["inbox_state"] for spec in all_specs.json()} == {"active", "archived"}
    assert next(spec for spec in all_specs.json() if spec["id"] == archived.id)["archive_reason"] == "implemented"
    assert client.get("/specs", params={"inbox": "active", "q": "needle"}).json()[0]["id"] == active.id
    assert client.get("/specs", params={"inbox": "archived"}).json()[0]["id"] == archived.id
    assert client.get("/specs", params={"inbox": "unknown"}).status_code == 422


def test_spec_inbox_mutations_are_ordered_idempotent_and_non_destructive(tmp_path: Path):
    active, _ = _seed(tmp_path)
    client = _client(tmp_path)

    first = client.post(f"/specs/{active.id}/inbox/archive", json={"mutation_id": "phone-archive-1"})
    retry = client.post(f"/specs/{active.id}/inbox/archive", json={"mutation_id": "phone-archive-1"})
    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()
    assert first.json()["inbox_state"] == "archived"
    assert client.post(f"/specs/{active.id}/inbox/pin", json={"mutation_id": "desktop-pin-1"}).json()["inbox_state"] == "active"
    assert client.post(f"/specs/{active.id}/inbox/unpin", json={"mutation_id": "phone-unpin-1"}).json()["inbox_state"] == "archived"
    assert client.post(f"/specs/{active.id}/inbox/restore", json={"mutation_id": "desktop-restore-1"}).json()["inbox_state"] == "active"
    assert SpecStore(tmp_path / "specs").find_by_id(active.id) is not None
    assert client.post(f"/specs/{active.id}/inbox/archive", json={"mutation_id": " "}).status_code == 422
    assert client.post("/specs/nope/inbox/archive", json={"mutation_id": "missing-1"}).status_code == 404
    assert client.post(f"/specs/{active.id}/inbox/nope", json={"mutation_id": "bad-1"}).status_code == 422


def test_spec_duplicate_artifacts_are_conflicts_for_review_and_inbox(tmp_path: Path):
    active, _ = _seed(tmp_path)
    canonical = next((tmp_path / "specs").glob("*-active.md"))
    (tmp_path / "specs" / "renamed.md").write_bytes(canonical.read_bytes())
    client = _client(tmp_path)

    for response in (
        client.get(f"/specs/{active.id}"),
        client.get(f"/specs/{active.id}/review"),
        client.post(
            f"/specs/{active.id}/inbox/archive",
            json={"mutation_id": "duplicate-archive"},
        ),
    ):
        assert response.status_code == 409
        assert "multiple physical artifacts" in response.json()["detail"]


def test_malformed_exact_spec_is_a_controlled_storage_conflict(tmp_path: Path):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / f"{datetime.now(timezone.utc):%Y-%m-%d}-malformed.md").write_text(
        "not a spec"
    )
    client = _client(tmp_path)

    for response in (
        client.get("/specs/malformed"),
        client.get("/specs/malformed/review"),
        client.post(
            "/specs/malformed/inbox/archive",
            json={"mutation_id": "malformed-archive"},
        ),
        client.post("/specs", json={"id": "malformed", "title": "Replacement"}),
    ):
        assert response.status_code == 409
        assert "frontmatter" in response.json()["detail"]


def test_spec_state_equivalent_inbox_action_persists_without_task_activity(tmp_path: Path):
    now = datetime.now(timezone.utc)
    state = StateManager(tmp_path / ".mothership")
    state.save(WorkspaceState(tasks={"spec-task": Task(
        slug="spec-task", description="d", phase="dev", created_at=now,
        affected_repos=[], branch="feat/spec-task",
    )}))
    active, _ = _seed(tmp_path)
    active.task_slug = "spec-task"
    specs = SpecStore(tmp_path / "specs")
    specs.save(active)
    client = TestClient(create_app(
        specs_dir=tmp_path / "specs", state_manager=state, log_manager=None,
        workspace_root=tmp_path, workspace_name="test-ws",
    ))

    client.post("/specs/active/inbox/archive", json={"mutation_id": "device-archive"})
    first_activity = state.load().tasks["spec-task"].last_activity_at
    first_mutation = specs.find_by_id(active.id).inbox.last_mutated_at
    response = client.post("/specs/active/inbox/unpin", json={"mutation_id": "device-unpin"})

    assert response.status_code == 200
    assert state.load().tasks["spec-task"].last_activity_at == first_activity
    saved = specs.find_by_id(active.id)
    assert saved.inbox.mutation_ids["device-unpin"] == "unpin"
    assert saved.inbox.last_mutated_at == first_mutation

def test_archive_uses_the_post_apply_spec_instead_of_a_stale_snapshot(
    tmp_path: Path, monkeypatch,
):
    from mship.core.spec_draft import SpecDraft, apply_draft_transaction

    now = datetime.now(timezone.utc)
    store = SpecStore(tmp_path / "specs")
    store.save(Spec(
        id="transition", title="Transition", status="approved",
        created_at=now, updated_at=now,
        acceptance_criteria=[],
        body="## Problem\n\nOld\n",
    ))
    archive_waiting = Event()
    allow_archive = Event()
    original_locked = SpecStore.locked

    @contextmanager
    def pause_archive_lock(self, spec_id):
        if current_thread().name != "MainThread":
            archive_waiting.set()
            assert allow_archive.wait(timeout=5)
        with original_locked(self, spec_id) as artifact:
            yield artifact

    monkeypatch.setattr(SpecStore, "locked", pause_archive_lock)
    client = _client(tmp_path)
    archive_result: dict[str, object] = {}

    def archive() -> None:
        archive_result["response"] = client.post("/specs/transition/archive")

    archive_thread = Thread(target=archive, name="archive-writer")
    archive_thread.start()
    assert archive_waiting.wait(timeout=5)

    applied = apply_draft_transaction(
        store,
        "transition",
        SpecDraft(
            problem="New problem",
            user_story="New story",
            approach="New approach",
        ),
        bypass_status_gate=True,
    )
    assert applied.spec.id == "transition"
    allow_archive.set()
    archive_thread.join(timeout=5)
    assert not archive_thread.is_alive()

    response = archive_result["response"]
    assert response.status_code == 200
    persisted = store.find_by_id("transition")
    assert persisted is not None
    assert persisted.status == "archived"
    assert "New approach" in persisted.body
