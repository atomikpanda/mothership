from contextlib import contextmanager
from threading import Event, Thread

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mship.core.message_store import MessageStore
from mship.core.serve import create_app
from mship.core.spec import AcceptanceCriterion, OpenQuestion, Spec
from mship.core.spec_body import render_body
from mship.core.spec_store import SpecStore
from mship.core.spec_storage import SpecStorage
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


def test_health_carries_daemon_workspace_identity_when_provided(tmp_path):
    state = StateManager(tmp_path / ".mothership")
    app = create_app(
        specs_dir=tmp_path / "specs",
        state_manager=state,
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
        host_id="host-a",
        workspace_id="ws-a",
    )

    assert TestClient(app).get("/health").json() == {
        "status": "ok",
        "workspace": "test-ws",
        "host_id": "host-a",
        "workspace_id": "ws-a",
    }


def test_list_specs(tmp_path):
    _seed_spec(tmp_path)
    r = TestClient(_app(tmp_path)).get("/specs")
    assert r.status_code == 200
    assert r.json() == [{
        "id": "dq", "title": "Decision queue", "status": "needs_review",
        "task_slug": "dq", "affected_repos": [], "locked": False,
        "inbox_state": "active", "archive_reason": None, "pinned": False,
    }]


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


def _write_plan(tmp_path: Path, name: str, body: str) -> Path:
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / name
    path.write_text(body)
    return path


def test_get_plan_assumptions_pending_flag(tmp_path):
    from mship.core.assumptions import AssumptionStore
    from mship.core.plan_check import (
        Flag, PlanCheckResult, PlanCheckStore, assumptions_hash, plan_hash,
    )

    sm, log = _seed_task(tmp_path)
    rows = AssumptionStore(tmp_path).seed()
    plan_path = _write_plan(tmp_path, "2026-07-30-dq.md", "# Plan\n\nSome body.\n")
    PlanCheckStore(tmp_path / ".mothership").save(PlanCheckResult(
        task_slug="dq",
        plan_hash=plan_hash(plan_path.read_text()),
        assumptions_hash=assumptions_hash(rows),
        verdicts=[],
        flags=[Flag(axis="repo topology", source="checker", reason="not addressed")],
    ))

    client = TestClient(_app_with(tmp_path, sm, log))
    body = client.get("/plan-assumptions/dq").json()
    assert body == {
        "task": "dq",
        "fresh": True,
        "pending": 1,
        "flags": [{
            "axis": "repo topology", "source": "checker", "reason": "not addressed",
            "axis_fingerprint": None,
            "approved": False, "approved_by": None, "approved_reason": None,
        }],
    }


def test_get_plan_assumptions_absent_result(tmp_path):
    sm, log = _seed_task(tmp_path)
    client = TestClient(_app_with(tmp_path, sm, log))
    assert client.get("/plan-assumptions/dq").json() == {
        "task": "dq", "fresh": False, "pending": 0, "flags": [],
    }
    assert client.get("/plan-assumptions/nope").status_code == 404


def test_post_plan_assumptions_approve_marks_flag_and_returns_envelope(tmp_path):
    from mship.core.assumptions import AssumptionStore
    from mship.core.plan_check import (
        Flag, PlanCheckResult, PlanCheckStore, assumptions_hash, plan_hash,
    )
    sm, log = _seed_task(tmp_path)
    rows = AssumptionStore(tmp_path).seed()
    plan_path = _write_plan(tmp_path, "2026-07-30-dq.md", "# Plan\n\nBody.\n")
    PlanCheckStore(tmp_path / ".mothership").save(PlanCheckResult(
        task_slug="dq", plan_hash=plan_hash(plan_path.read_text()),
        assumptions_hash=assumptions_hash(rows), verdicts=[],
        flags=[Flag(axis="repo topology", source="checker", reason="gap")],
    ))
    client = TestClient(_app_with(tmp_path, sm, log))
    body = client.post("/plan-assumptions/dq/approve",
                       json={"axis": "repo topology", "reason": "ok"}).json()
    assert body["pending"] == 0
    flag = body["flags"][0]
    assert flag["approved"] is True
    assert flag["approved_by"] == "operator"
    assert flag["approved_reason"] == "ok"


def test_get_plan_assumptions_list_returns_pending_per_task(tmp_path):
    from mship.core.assumptions import AssumptionStore
    from mship.core.plan_check import Flag, PlanCheckResult, PlanCheckStore, assumptions_hash, plan_hash
    sm, log = _seed_task(tmp_path)          # task "dq"
    rows = AssumptionStore(tmp_path).seed()
    plan_path = _write_plan(tmp_path, "2026-07-30-dq.md", "# Plan\n\nBody.\n")
    PlanCheckStore(tmp_path / ".mothership").save(PlanCheckResult(
        task_slug="dq", plan_hash=plan_hash(plan_path.read_text()),
        assumptions_hash=assumptions_hash(rows), verdicts=[],
        flags=[Flag(axis="repo topology", source="checker", reason="gap")]))
    client = TestClient(_app_with(tmp_path, sm, log))
    body = client.get("/plan-assumptions").json()
    row = next(r for r in body if r["task"] == "dq")
    assert row["pending"] == 1 and row["fresh"] is True


def test_get_plan_assumptions_list_tolerates_one_task_unreadable_plan(tmp_path):
    """A task's plan file can go bad-encoding/unreadable between the stored
    plan-check lingering and the plan being pruned (e.g. `mship close`) —
    that task's summary must be skipped, not blow up the WHOLE list (500),
    which would blank the Queue cards for every other task too (Greptile P1,
    Wave 3c)."""
    from mship.core.assumptions import AssumptionStore
    from mship.core.plan_check import Flag, PlanCheckResult, PlanCheckStore, assumptions_hash, plan_hash

    state_dir = tmp_path / ".mothership"
    state_dir.mkdir(exist_ok=True)
    sm = StateManager(state_dir)
    sm.save(WorkspaceState(tasks={
        "dq": Task(slug="dq", description="d", phase="dev",
                   created_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
                   affected_repos=["mothership"], branch="feat/dq"),
        "ok": Task(slug="ok", description="d", phase="dev",
                   created_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
                   affected_repos=["mothership"], branch="feat/ok"),
    }))
    log = LogManager(state_dir / "logs")

    rows = AssumptionStore(tmp_path).seed()

    # "dq"'s plan file is present but unreadable text (bad encoding) — the
    # race the finding describes (plan removed/corrupted after the check was
    # recorded, before the list endpoint reads it).
    bad_plan = _write_plan(tmp_path, "2026-07-30-dq.md", "placeholder")
    bad_plan.write_bytes(b"\xff\xfe not valid utf-8")
    PlanCheckStore(tmp_path / ".mothership").save(PlanCheckResult(
        task_slug="dq", plan_hash="irrelevant",
        assumptions_hash=assumptions_hash(rows), verdicts=[],
        flags=[Flag(axis="repo topology", source="checker", reason="gap")],
    ))

    # "ok" is a perfectly healthy task with a fresh, readable plan.
    ok_plan = _write_plan(tmp_path, "2026-07-30-ok.md", "# Plan\n\nBody.\n")
    PlanCheckStore(tmp_path / ".mothership").save(PlanCheckResult(
        task_slug="ok", plan_hash=plan_hash(ok_plan.read_text()),
        assumptions_hash=assumptions_hash(rows), verdicts=[],
        flags=[Flag(axis="repo topology", source="checker", reason="gap")],
    ))

    client = TestClient(_app_with(tmp_path, sm, log))
    r = client.get("/plan-assumptions")
    assert r.status_code == 200
    body = r.json()
    slugs = [row["task"] for row in body]
    assert "dq" not in slugs
    row = next(row for row in body if row["task"] == "ok")
    assert row["pending"] == 1 and row["fresh"] is True


def test_post_plan_assumptions_approve_unknown_axis_404(tmp_path):
    from mship.core.assumptions import AssumptionStore
    from mship.core.plan_check import (
        Flag, PlanCheckResult, PlanCheckStore, assumptions_hash, plan_hash,
    )
    sm, log = _seed_task(tmp_path)
    rows = AssumptionStore(tmp_path).seed()
    _write_plan(tmp_path, "2026-07-30-dq.md", "# Plan\n\nBody.\n")
    PlanCheckStore(tmp_path / ".mothership").save(PlanCheckResult(
        task_slug="dq", plan_hash=plan_hash("# Plan\n\nBody.\n"),
        assumptions_hash=assumptions_hash(rows), verdicts=[],
        flags=[Flag(axis="repo topology", source="checker", reason="gap")],
    ))
    client = TestClient(_app_with(tmp_path, sm, log))
    r = client.post("/plan-assumptions/dq/approve", json={"axis": "no such axis"})
    assert r.status_code == 404


def test_post_plan_assumptions_approve_stale_check_409(tmp_path):
    """The stored check was recorded against an OLDER plan text than what's on
    disk now — the server must refuse the approve (409), not silently accept
    it and leave the plan->dev gate (which uses is_fresh) still blocked
    (Greptile #453)."""
    from mship.core.assumptions import AssumptionStore
    from mship.core.plan_check import (
        Flag, PlanCheckResult, PlanCheckStore, assumptions_hash, plan_hash,
    )
    sm, log = _seed_task(tmp_path)
    rows = AssumptionStore(tmp_path).seed()
    plan_path = _write_plan(tmp_path, "2026-07-30-dq.md", "# Plan\n\nBody.\n")
    PlanCheckStore(tmp_path / ".mothership").save(PlanCheckResult(
        task_slug="dq", plan_hash=plan_hash("# Plan\n\nOLD body the check ran against.\n"),
        assumptions_hash=assumptions_hash(rows), verdicts=[],
        flags=[Flag(axis="repo topology", source="checker", reason="gap")],
    ))
    # plan_path on disk ("# Plan\n\nBody.\n") no longer matches the hash the
    # check was recorded against ("...OLD body...")

    client = TestClient(_app_with(tmp_path, sm, log))
    r = client.post("/plan-assumptions/dq/approve", json={"axis": "repo topology"})
    assert r.status_code == 409

    stored = PlanCheckStore(tmp_path / ".mothership").get("dq")
    assert stored.flags[0].approved is False  # not mutated by the rejected approve


def test_post_plan_assumptions_approve_unknown_task_404(tmp_path):
    sm, log = _seed_task(tmp_path)
    client = TestClient(_app_with(tmp_path, sm, log))
    r = client.post("/plan-assumptions/nope/approve", json={"axis": "x"})
    assert r.status_code == 404


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


def test_post_prose_verdict(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/prose-verdict", json={"section_id": "approach", "verdict": "flagged", "comment": "unclear"})
    assert r.status_code == 200
    # verdict endpoint returns the review; the spec itself now carries the prose verdict
    from mship.core.spec_store import SpecStore
    s = SpecStore(tmp_path / "specs").find_by_id("dq")
    assert s.prose_verdicts["approach"].verdict == "flagged"
    assert s.prose_verdicts["approach"].comment == "unclear"
    assert client.post("/specs/dq/prose-verdict", json={"section_id": "nope", "verdict": "approved"}).status_code == 400
    assert client.post("/specs/dq/prose-verdict", json={"section_id": "approach", "verdict": "bogus"}).status_code == 400


def test_post_verdict_with_comment(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/verdict", json={"criterion_id": "ac1", "verdict": "flagged", "comment": "fix"})
    assert r.status_code == 200
    from mship.core.spec_store import SpecStore
    assert SpecStore(tmp_path / "specs").find_by_id("dq").acceptance_criteria[0].comment == "fix"


def test_answer_clearing_last_blocker_auto_approves(tmp_path):
    # "dq" already has ac1 approved and no prose blockers; its only remaining blocker is the
    # unanswered q1. Answering it clears the gate, so the spec auto-approves (and persists) —
    # a fully-reviewed spec no longer sits stuck in needs_review.
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/questions/q1/answer", json={"answer": "yes"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    assert SpecStore(tmp_path / "specs").find_by_id("dq").status == "approved"


def test_verdict_clearing_last_blocker_auto_approves(tmp_path):
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id="s2", title="s2", status="needs_review", created_at=now, updated_at=now, task_slug="s2",
        body=render_body("p", "u", "a"),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="unreviewed")],
        open_questions=[],
    ))
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/s2/verdict", json={"criterion_id": "ac1", "verdict": "approved"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"                                      # sole blocker cleared
    assert SpecStore(tmp_path / "specs").find_by_id("s2").status == "approved"


def test_verdict_not_yet_clearing_blockers_stays_needs_review(tmp_path):
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id="s3", title="s3", status="needs_review", created_at=now, updated_at=now, task_slug="s3",
        body=render_body("p", "u", "a"),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="unreviewed"),
                             AcceptanceCriterion(id="ac2", text="y", verdict="unreviewed")],
        open_questions=[],
    ))
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/s3/verdict", json={"criterion_id": "ac1", "verdict": "approved"})
    assert r.status_code == 200
    assert r.json()["status"] == "needs_review"                                  # ac2 still unreviewed


def test_post_evidence_persists_and_validates(tmp_path):
    _seed_spec(tmp_path)   # spec "dq" with one AC "ac1"
    client = TestClient(_app(tmp_path))

    r = client.post("/specs/dq/evidence", json={"criterion_id": "ac1", "ref": "test-runs/5"})
    assert r.status_code == 200
    # build_review surfacing is Task 5; verify persistence via the full spec dump.
    ac = client.get("/specs/dq").json()["acceptance_criteria"][0]
    assert ac["evidence"] == [{"kind": "test", "ref": "test-runs/5", "note": None}]

    # explicit kind override + note round-trips
    r2 = client.post(
        "/specs/dq/evidence",
        json={"criterion_id": "ac1", "ref": "HEAD", "kind": "commit", "note": "fix"},
    )
    assert r2.status_code == 200
    ev = client.get("/specs/dq").json()["acceptance_criteria"][0]["evidence"]
    assert ev[1] == {"kind": "commit", "ref": "HEAD", "note": "fix"}

    # bad criterion → 400; unknown spec → 404
    assert client.post("/specs/dq/evidence", json={"criterion_id": "nope", "ref": "x"}).status_code == 400
    assert client.post("/specs/none/evidence", json={"criterion_id": "ac1", "ref": "x"}).status_code == 404


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
    # clearing the last blocker (answering q1) auto-approves — no separate /approve needed
    ans = client.post("/specs/dq/questions/q1/answer", json={"answer": "yes"})
    assert ans.status_code == 200 and ans.json()["status"] == "approved"
    # a redundant /approve on the already-approved spec is idempotent (200), not a spurious 409
    reapprove = client.post("/specs/dq/approve", json={})
    assert reapprove.status_code == 200 and reapprove.json()["status"] == "approved"


def test_post_approve_succeeds_when_approvable_but_not_auto_approved(tmp_path):
    # The explicit /approve still works for an approvable spec that never went through a verdict
    # endpoint (so auto-approve didn't fire) — e.g. one seeded already-approvable.
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id="ready", title="ready", status="needs_review", created_at=now, updated_at=now, task_slug="ready",
        body=render_body("p", "u", "a"),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="approved")],
        open_questions=[],
    ))
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/ready/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "approved"


def test_post_approve_bypass(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/approve", json={"bypass_gate": True})
    assert r.status_code == 200 and r.json()["status"] == "approved"


def test_post_approve_stale_revision_returns_conflict_without_mutating(tmp_path, monkeypatch):
    import mship.core.serve as serve_mod

    _seed_spec(tmp_path)
    store = SpecStore(tmp_path / "specs")
    newer = {}
    original = serve_mod.approve_spec

    def stale(spec, store, *, bypass_gate=False):
        current = store.find_by_id(spec.id)
        current.updated_at = spec.updated_at + timedelta(seconds=1)
        store.save(current)
        newer["state"] = current.model_dump()
        return original(spec, store, bypass_gate=bypass_gate)

    monkeypatch.setattr(serve_mod, "approve_spec", stale)

    response = TestClient(_app(tmp_path)).post(
        "/specs/dq/approve", json={"bypass_gate": True},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "spec revision conflict for 'dq'; reload and retry"
    assert "Traceback" not in response.text
    assert store.find_by_id("dq").model_dump() == newer["state"]


def test_post_request_changes(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/request-changes", json={"reason": "tighten scope"})
    # MOS-240: request-changes -> editable `draft` status carrying the reason.
    assert r.status_code == 200 and r.json()["status"] == "draft"


def test_post_request_changes_stale_revision_returns_conflict_without_mutating(
    tmp_path, monkeypatch,
):
    import mship.core.serve as serve_mod

    _seed_spec(tmp_path)
    store = SpecStore(tmp_path / "specs")
    newer = {}
    original = serve_mod.request_changes_spec

    def stale(spec, store, reason, *, log_manager, actor, now=None):
        current = store.find_by_id(spec.id)
        current.updated_at = spec.updated_at + timedelta(seconds=1)
        store.save(current)
        newer["state"] = current.model_dump()
        return original(
            spec, store, reason, log_manager=log_manager, actor=actor, now=now,
        )

    monkeypatch.setattr(serve_mod, "request_changes_spec", stale)

    response = TestClient(_app(tmp_path)).post(
        "/specs/dq/request-changes", json={"reason": "tighten scope"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "spec revision conflict for 'dq'; reload and retry"
    assert "Traceback" not in response.text
    assert store.find_by_id("dq").model_dump() == newer["state"]


def test_post_request_changes_persists_reason(tmp_path):
    """MOS-215: the reason must land on the persisted spec, not just the
    review payload — verified by reloading via GET /specs/{id}."""
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/request-changes", json={"reason": "tighten scope"})
    assert r.status_code == 200
    assert r.json()["clarification_reason"] == "tighten scope"
    assert client.get("/specs/dq").json()["clarification_reason"] == "tighten scope"


def test_post_request_changes_records_durable_rejection(tmp_path):
    """#447: the serve request-changes handler must also append a durable,
    append-only `rejected` journal event (actor="operator"), independent of
    `clarification_reason` on the spec (which approve_spec later nulls)."""
    import json

    _seed_spec(tmp_path)
    log = LogManager(tmp_path / ".mothership" / "logs")
    client = TestClient(_app_with(tmp_path, StateManager(tmp_path / ".mothership"), log))
    r = client.post("/specs/dq/request-changes", json={"reason": "tighten scope"})
    assert r.status_code == 200

    rejected = [e for e in log.read("dq") if e.action == "rejected"]
    assert len(rejected) == 1
    payload = json.loads(rejected[0].message)
    assert payload == {"actor": "operator", "reason": "tighten scope"}


def test_post_request_changes_fails_loud_when_journal_write_fails(tmp_path, monkeypatch):
    """#447 review: a durable-write failure must propagate (not be swallowed)
    and must leave the spec's status untouched — record-then-transition
    ordering means a failed append happens before the spec ever flips to
    draft, so the operator sees a real error and can retry cleanly."""
    import mship.core.spec_transition as st

    _seed_spec(tmp_path)
    log = LogManager(tmp_path / ".mothership" / "logs")
    client = TestClient(_app_with(tmp_path, StateManager(tmp_path / ".mothership"), log))

    def _boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(st, "record_rejection", _boom)

    with pytest.raises(OSError):
        client.post("/specs/dq/request-changes", json={"reason": "tighten scope"})

    assert SpecStore(tmp_path / "specs").find_by_id("dq").status == "needs_review"


def test_post_request_changes_rejects_empty_reason(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/request-changes", json={"reason": "   "})
    assert r.status_code == 400
    assert SpecStore(tmp_path / "specs").find_by_id("dq").status == "needs_review"


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



def test_post_create_spec_unsafe_id_is_422(tmp_path):
    response = TestClient(_app(tmp_path)).post("/specs", json={"title": "Anything", "id": "../unsafe"})

    assert response.status_code == 422

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


def test_post_apply_refuses_invalid_transition_without_mutating_spec(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    before = SpecStore(tmp_path / "specs").find_by_id("dq")
    assert before is not None

    response = client.post("/specs/dq/apply", json={"draft": {
        "problem": "replacement", "user_story": "U", "approach": "A",
        "acceptance_criteria": ["replacement"],
    }})

    assert response.status_code == 409
    assert "traceback" not in response.text.lower()
    after = SpecStore(tmp_path / "specs").find_by_id("dq")
    assert after is not None
    assert after.model_dump(mode="json") == before.model_dump(mode="json")


def test_post_apply_bypass_still_refuses_review_discard_without_mutation(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    before = SpecStore(tmp_path / "specs").find_by_id("dq")
    assert before is not None

    response = client.post("/specs/dq/apply", json={
        "draft": {
            "problem": "replacement", "user_story": "U", "approach": "A",
            "acceptance_criteria": ["replacement"],
        },
        "bypass_status_gate": True,
    })

    assert response.status_code == 409
    assert "discard" in response.json()["detail"]
    assert "traceback" not in response.text.lower()
    after = SpecStore(tmp_path / "specs").find_by_id("dq")
    assert after is not None
    assert after.model_dump(mode="json") == before.model_dump(mode="json")


def test_post_apply_discard_review_reports_redacted_discard_state(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))

    response = client.post("/specs/dq/apply", json={
        "draft": {
            "problem": "replacement", "user_story": "U", "approach": "A",
            "acceptance_criteria": ["replacement"],
        },
        "bypass_status_gate": True,
        "discard_review": True,
    })

    assert response.status_code == 200
    assert response.json()["status"] == "needs_review"
    assert response.json()["review_state_discarded"] is True
    assert response.json()["discarded_review_count"] == 1


def test_post_apply_preserves_unchanged_review_comment_and_answer(tmp_path):
    _seed_spec(tmp_path)
    store = SpecStore(tmp_path / "specs")
    spec = store.find_by_id("dq")
    assert spec is not None
    spec.acceptance_criteria[0].comment = "clear enough"
    spec.open_questions[0].answer = "yes"
    store.save(spec)
    client = TestClient(_app(tmp_path))

    response = client.post("/specs/dq/apply", json={
        "draft": {
            "problem": "the problem", "user_story": "as a user", "approach": "the approach",
            "acceptance_criteria": ["view questions"],
            "open_questions": ["Mobile too?"],
        },
        "bypass_status_gate": True,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["review_state_discarded"] is False
    assert body["discarded_review_count"] == 0
    assert body["acceptance_criteria"][0]["comment"] == "clear enough"
    assert body["open_questions"][0]["answer"] == "yes"


def test_review_write_waits_for_atomic_apply_and_uses_fresh_spec(tmp_path, monkeypatch):
    _seed_spec(tmp_path)
    app = _app(tmp_path)
    review_started = Event()
    review_request_active = Event()
    review_lock_acquired = Event()
    review_responses = []
    reviewer: Thread | None = None
    original_locked = SpecStore.locked
    original_save_while_locked = SpecStore.save_while_locked

    @contextmanager
    def track_reviewer_lock(self, spec_id):
        is_reviewer = review_request_active.is_set()
        if is_reviewer:
            review_started.set()
        with original_locked(self, spec_id) as artifact:
            if is_reviewer:
                review_lock_acquired.set()
            yield artifact

    def post_review():
        review_request_active.set()
        response = TestClient(app).post(
            "/specs/dq/verdict",
            json={"criterion_id": "ac1", "verdict": "approved", "comment": "late review"},
        )
        review_responses.append(response)

    def pause_apply_save(self, spec, artifact):
        nonlocal reviewer
        if reviewer is None:
            reviewer = Thread(target=post_review, name="api-reviewer")
            reviewer.start()
            assert review_started.wait(timeout=1)
            assert not review_lock_acquired.wait(timeout=0.1)
        return original_save_while_locked(self, spec, artifact)

    monkeypatch.setattr(SpecStore, "locked", track_reviewer_lock)
    monkeypatch.setattr(SpecStore, "save_while_locked", pause_apply_save)

    response = TestClient(app).post("/specs/dq/apply", json={
        "draft": {
            "problem": "replacement", "user_story": "as a user", "approach": "the approach",
            "acceptance_criteria": ["view questions"],
            "open_questions": ["Mobile too?"],
        },
        "bypass_status_gate": True,
    })

    assert response.status_code == 200
    assert reviewer is not None
    reviewer.join(timeout=1)
    assert not reviewer.is_alive()
    assert review_lock_acquired.is_set()
    assert review_responses[0].status_code == 200
    persisted = SpecStore(tmp_path / "specs").find_by_id("dq")
    assert persisted is not None
    assert persisted.acceptance_criteria[0].comment == "late review"
    assert persisted.body == render_body("replacement", "as a user", "the approach")

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



@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/threads/.hidden", None),
        ("post", "/threads/.hidden/messages", {"text": "x"}),
        ("post", "/threads/.hidden/seen", {}),
    ],
)
def test_unsafe_thread_ids_are_controlled_4xx(tmp_path, method, path, body):
    client = TestClient(_app(tmp_path))
    response = client.get(path) if method == "get" else client.post(path, json=body)
    assert 400 <= response.status_code < 500

def test_threads_filter_search_and_mutate_inbox_without_deleting_content(tmp_path):
    now = datetime.now(timezone.utc)
    messages = MessageStore(tmp_path / ".mothership" / "messages")
    archived = messages.create_thread("Old discussion", "needle in history", now.replace(year=now.year - 1))
    messages.append(archived.id, "agent", "resolved", archived.updated_at)
    active = messages.create_thread("Current discussion", "needle today", now)
    messages.append(active.id, "agent", "needle resolved", now)
    client = TestClient(_app(tmp_path))

    all_threads = client.get("/threads").json()
    assert {thread["id"] for thread in all_threads} == {archived.id, active.id}
    assert {thread["inbox_state"] for thread in all_threads} == {"active", "archived"}
    assert next(thread for thread in all_threads if thread["id"] == archived.id)["archive_reason"] == "inactive_unlinked"
    assert client.get("/threads", params={"inbox": "active", "q": "needle"}).json()[0]["id"] == active.id
    assert client.get("/threads", params={"inbox": "archived"}).json()[0]["id"] == archived.id
    assert client.get("/threads", params={"inbox": "unknown"}).status_code == 422

    first = client.post(
        f"/threads/{active.id}/inbox/archive", json={"mutation_id": "phone-archive-1"},
    )
    retry = client.post(
        f"/threads/{active.id}/inbox/archive", json={"mutation_id": "phone-archive-1"},
    )
    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()
    assert first.json()["inbox_state"] == "archived"
    assert client.get(f"/threads/{active.id}").json()["messages"][0]["text"] == "needle today"
    assert client.get("/threads", params={"inbox": "all", "q": "current"}).json()[0]["id"] == active.id
    assert client.post(f"/threads/{active.id}/inbox/pin", json={"mutation_id": "desktop-pin-1"}).json()["inbox_state"] == "active"
    assert client.post(f"/threads/{active.id}/inbox/unpin", json={"mutation_id": "phone-unpin-1"}).json()["inbox_state"] == "archived"
    assert client.post(f"/threads/{active.id}/inbox/restore", json={"mutation_id": "desktop-restore-1"}).json()["inbox_state"] == "active"
    assert client.post(f"/threads/{active.id}/inbox/archive", json={"mutation_id": " "}).status_code == 422
    assert client.post("/threads/nope/inbox/archive", json={"mutation_id": "missing-1"}).status_code == 404
    assert client.post(f"/threads/{active.id}/inbox/nope", json={"mutation_id": "bad-1"}).status_code == 422


def test_thread_state_equivalent_inbox_action_persists_without_task_activity(tmp_path):
    state, log = _seed_task(tmp_path)
    now = datetime.now(timezone.utc)
    messages = MessageStore(tmp_path / ".mothership" / "messages")
    thread = messages.create_thread("task thread", "body", now, task_slug="dq")
    client = TestClient(_app_with(tmp_path, state, log))

    client.post(f"/threads/{thread.id}/inbox/archive", json={"mutation_id": "device-archive"})
    first_activity = state.load().tasks["dq"].last_activity_at
    first_mutation = messages.get(thread.id).inbox.last_mutated_at
    response = client.post(f"/threads/{thread.id}/inbox/unpin", json={"mutation_id": "device-unpin"})

    assert response.status_code == 200
    assert state.load().tasks["dq"].last_activity_at == first_activity
    saved = messages.get(thread.id)
    assert saved.inbox.mutation_ids["device-unpin"] == "unpin"
    assert saved.inbox.last_mutated_at == first_mutation



def test_thread_linked_to_done_work_item_is_archived(tmp_path):
    now = datetime.now(timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id="implemented", title="Implemented", status="implemented",
        created_at=now, updated_at=now,
    ))
    messages = MessageStore(tmp_path / ".mothership" / "messages")
    thread = messages.create_thread("Linked", "content", now)
    messages.append(thread.id, "agent", "resolved", now)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    item = items.create("Implemented", "feature", "test-ws", now)
    items.link_spec(item.id, "implemented", now)
    items.add_thread(item.id, thread.id, now)

    summary = TestClient(_app(tmp_path)).get("/threads", params={"inbox": "archived"}).json()[0]

    assert summary["id"] == thread.id
    assert summary["inbox_state"] == "archived"
    assert summary["archive_reason"] == "linked_terminal"


def test_malformed_canonical_spec_keeps_linked_terminal_thread_active(tmp_path):
    now = datetime.now(timezone.utc)
    spec = Spec(
        id="implemented", title="Implemented", status="implemented",
        created_at=now, updated_at=now,
    )
    path = SpecStore(tmp_path / "specs").save(spec)
    messages = MessageStore(tmp_path / ".mothership" / "messages")
    thread = messages.create_thread("Linked", "content", now)
    messages.append(thread.id, "agent", "resolved", now)
    messages.link_spec(thread.id, spec.id)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    item = items.create("Implemented", "feature", "test-ws", now)
    items.link_spec(item.id, spec.id, now)
    path.write_text("not a valid spec")

    summary = TestClient(_app(tmp_path)).get("/threads", params={"inbox": "all"}).json()[0]

    assert summary["work_item_id"] == item.id
    assert summary["inbox_state"] == "active"
    assert summary["archive_reason"] is None


def test_renamed_locked_spec_alias_keeps_linked_terminal_thread_active(tmp_path):
    now = datetime.now(timezone.utc)
    spec = Spec(
        id="implemented",
        title="Implemented",
        status="implemented",
        created_at=now,
        updated_at=now,
    )
    SpecStore(tmp_path / "specs").save(spec)
    encrypted_root = tmp_path / "encrypted-source"
    encrypted_storage = SpecStorage(
        encrypted_root / "specs",
        mode="encrypted",
        workspace_root=encrypted_root,
    )
    encrypted_path = SpecStore(
        encrypted_root / "specs",
        storage=encrypted_storage,
    ).save(spec)
    (tmp_path / "specs" / "legacy.md.enc").write_bytes(encrypted_path.read_bytes())

    messages = MessageStore(tmp_path / ".mothership" / "messages")
    thread = messages.create_thread("Linked", "content", now)
    messages.append(thread.id, "agent", "resolved", now)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    item = items.create("Implemented", "feature", "test-ws", now)
    items.link_spec(item.id, spec.id, now)
    items.add_thread(item.id, thread.id, now)

    summary = TestClient(_app(tmp_path)).get("/threads", params={"inbox": "all"}).json()[0]

    assert summary["work_item_id"] == item.id
    assert summary["inbox_state"] == "active"
    assert summary["archive_reason"] is None


def test_threads_keep_healthy_terminal_link_with_corrupt_work_item(tmp_path):
    now = datetime.now(timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id="implemented", title="Implemented", status="implemented",
        created_at=now, updated_at=now,
    ))
    messages = MessageStore(tmp_path / ".mothership" / "messages")
    thread = messages.create_thread("Linked", "content", now)
    messages.append(thread.id, "agent", "resolved", now)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    item = items.create("Implemented", "feature", "test-ws", now)
    items.link_spec(item.id, "implemented", now)
    items.add_thread(item.id, thread.id, now)
    (tmp_path / ".mothership" / "workitems" / "corrupt.json").write_text("{")

    response = TestClient(_app(tmp_path)).get("/threads", params={"inbox": "all"})

    assert response.status_code == 200
    summary = next(summary for summary in response.json() if summary["id"] == thread.id)
    assert summary["work_item_id"] == item.id
    assert summary["inbox_state"] == "archived"
    assert summary["archive_reason"] == "linked_terminal"


def test_threads_keep_old_unknown_linkage_active_with_corrupt_work_item(tmp_path):
    now = datetime.now(timezone.utc)
    messages = MessageStore(tmp_path / ".mothership" / "messages")
    thread = messages.create_thread("Old unknown", "content", now.replace(year=now.year - 1))
    workitems_dir = tmp_path / ".mothership" / "workitems"
    workitems_dir.mkdir(parents=True)
    (workitems_dir / "corrupt.json").write_text("{")

    response = TestClient(_app(tmp_path)).get("/threads", params={"inbox": "all"})

    assert response.status_code == 200
    summary = next(summary for summary in response.json() if summary["id"] == thread.id)
    assert summary["work_item_id"] is None
    assert summary["inbox_state"] == "active"
    assert summary["archive_reason"] is None

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


def test_thread_detail_tolerates_an_unrelated_corrupt_spec(tmp_path):
    _seed_spec(tmp_path)
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    items = WorkItemStore(tmp_path / ".mothership" / "workitems")
    wi = items.create(title="Decision queue", kind="feature", workspace="test-ws", now=now)
    items.link_spec(wi.id, "dq", now=now)

    client = TestClient(_app(tmp_path))
    tid = client.post("/threads", json={"text": "hi"}).json()["id"]
    MessageStore(tmp_path / ".mothership" / "messages").link_spec(tid, "dq")
    (tmp_path / "specs" / "corrupt.md").write_text("{")

    response = client.get(f"/threads/{tid}")

    assert response.status_code == 200
    assert response.json()["work_item_id"] == wi.id
    assert response.json()["work_item"] == {
        "id": wi.id, "title": "Decision queue", "kind": "feature", "phase": "shaping",
    }


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


def test_get_task_serializes_activity_fields(tmp_path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir(exist_ok=True)
    sm = StateManager(state_dir)
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    sm.save(WorkspaceState(tasks={"dq": Task(
        slug="dq", description="d", phase="dev",
        created_at=datetime(2026, 6, 14, tzinfo=timezone.utc),
        affected_repos=["mothership"], branch="feat/dq",
        last_activity_at=now, phase_entered_at=now,
    )}))
    body = TestClient(_app(tmp_path)).get("/tasks/dq").json()
    assert body["last_activity_at"].startswith("2026-07-13T12:00:00")
    assert body["phase_entered_at"].startswith("2026-07-13T12:00:00")


def test_approve_endpoint_uses_shared_transition(tmp_path, monkeypatch):
    called = {}
    import mship.core.serve as serve_mod

    def spy(spec, store, *, bypass_gate=False):
        called["hit"] = (spec.id, bypass_gate)
        spec.status = "approved"
        spec.clarification_reason = None
        store.save(spec)
    monkeypatch.setattr(serve_mod, "approve_spec", spy, raising=False)

    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id="ready", title="ready", status="needs_review", created_at=now, updated_at=now,
        body=render_body("p", "u", "a"),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="x", verdict="approved")],
        open_questions=[]))
    r = TestClient(_app(tmp_path)).post("/specs/ready/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert called["hit"] == ("ready", False)
