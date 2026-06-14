# `mship serve` write endpoints (review → approve) — Plan (B1.5 / MOS-166)

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Add the review→approve POST endpoints to `mship serve`, each a thin wrapper over an existing core write helper, gated by the B2 token, returning the fresh `build_review` payload.

**Architecture:** Module-level pydantic request models in `core/serve.py`; POST routes added inside `create_app` (after the GET routes), using `_load_or_404` + `_save` closures; core errors mapped to HTTP (404/400/409). Design: [docs/superpowers/specs/2026-06-14-serve-writes-design.md](../specs/2026-06-14-serve-writes-design.md).

**Tech Stack:** FastAPI, pydantic, pytest. Builds on B1/B2 (`core/serve.py`) + the A1–A5 core helpers (all in `main`).

---

## File structure
- **Modify** `src/mship/core/serve.py` (request models + POST routes + helpers).
- **Test** `tests/core/test_serve.py` (TestClient write tests).

Reuses existing test helpers in `test_serve.py`: `_app(tmp_path)`, `_auth_app(tmp_path, token)`, `_seed_spec(tmp_path)` (seeds spec `dq`: `ac1` verdict `approved`, `q1` unanswered).

---

## Task 1: request models + verdict / questions / answer endpoints

**Files:** `src/mship/core/serve.py`; `tests/core/test_serve.py`.

- [ ] **Step 1: Failing tests** (append to `tests/core/test_serve.py`):

```python
def test_post_verdict(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/verdict", json={"criterion_id": "ac1", "verdict": "flagged"})
    assert r.status_code == 200
    assert r.json()["acceptance_criteria"][0]["verdict"] == "flagged"   # build_review payload
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
```

- [ ] **Step 2: Run → fail** (`uv run pytest tests/core/test_serve.py -k "verdict or question" -v` → 405/404, no POST routes).

- [ ] **Step 3: Implement.** Add module-level request models at the top of `src/mship/core/serve.py` (pydantic is already a dep; add `from pydantic import BaseModel`):

```python
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
```

Inside `create_app`, after the GET routes (and after the existing lazy `from fastapi import ...` — ensure `HTTPException` is imported there), add the helpers + 3 routes:

```python
    from datetime import datetime, timezone
    from mship.core.spec_review import set_criterion_verdict  # build_review already imported above
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
```

(`build_review` is imported in the GET section from Task-2-of-B1; if it isn't in scope where these handlers are defined, add `from mship.core.spec_review import build_review` here too.)

- [ ] **Step 4: Run → pass.** Then the whole `tests/core/test_serve.py`.
- [ ] **Step 5: Commit** `feat(serve): write endpoints — verdict, add/answer question` + journal.

---

## Task 2: approve / request-changes endpoints

**Files:** `src/mship/core/serve.py`; `tests/core/test_serve.py`.

- [ ] **Step 1: Failing tests** (append):

```python
def test_post_approve_gate_and_success(tmp_path):
    _seed_spec(tmp_path)   # ac1 approved, q1 unanswered → blocked on q1
    client = TestClient(_app(tmp_path))
    blocked = client.post("/specs/dq/approve", json={})
    assert blocked.status_code == 409
    client.post("/specs/dq/questions/q1/answer", json={"answer": "yes"})   # clear the blocker
    ok = client.post("/specs/dq/approve", json={})
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved"
    # re-approve from approved → illegal transition → 409
    assert client.post("/specs/dq/approve", json={}).status_code == 409


def test_post_approve_bypass(tmp_path):
    _seed_spec(tmp_path)   # still blocked (q1 unanswered)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/approve", json={"bypass_gate": True})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"


def test_post_request_changes(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_app(tmp_path))
    r = client.post("/specs/dq/request-changes", json={"reason": "tighten scope"})
    assert r.status_code == 200
    assert r.json()["status"] == "needs_clarification"
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — inside `create_app`, after the Task 1 routes:

```python
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
```

- [ ] **Step 4: Run → pass.** Then the whole `tests/core/test_serve.py`.
- [ ] **Step 5: Commit** `feat(serve): write endpoints — approve + request-changes (gate, 409 on conflict)` + journal.

---

## Task 3: writes inherit auth + full suite

**Files:** `tests/core/test_serve.py`; (verification).

- [ ] **Step 1: Test** — writes are gated by the B2 token (inherited app-level dependency):

```python
def test_writes_require_auth(tmp_path):
    _seed_spec(tmp_path)
    client = TestClient(_auth_app(tmp_path, "secret"))
    # No Authorization header → 401 even for a write.
    assert client.post("/specs/dq/verdict", json={"criterion_id": "ac1", "verdict": "approved"}).status_code == 401
    # With the token → allowed.
    ok = client.post("/specs/dq/verdict", json={"criterion_id": "ac1", "verdict": "approved"},
                     headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
```

- [ ] **Step 2: Run → pass** (auth dependency already applies to all routes; this confirms it covers POSTs).
- [ ] **Step 3: Full suite.** `mship test` (or `uv run pytest`) → green.
- [ ] **Step 4: Commit** `test(serve): confirm write endpoints inherit bearer auth; full suite green` + journal.

---

## Self-Review
- **Coverage:** verdict / add-question / answer (T1); approve + gate + 409 / request-changes (T2); auth-gating + full suite (T3). Each returns `build_review`.
- **Error mapping:** spec 404; domain `ValueError` → 400; `InvalidTransition` + gate-blocked → 409; malformed body → 422 (FastAPI). All tested.
- **Placeholders:** none — complete code. The one in-scope note (ensure `build_review`/`HTTPException` in scope where the POST handlers are defined) is called out.
- **Type consistency:** request models (`VerdictBody`/`QuestionBody`/`AnswerBody`/`ApproveBody`/`ReasonBody`); reuses `set_criterion_verdict`, `add_question`, `answer_question`, `approval_blockers`, `validate_transition`, `InvalidTransition`, `build_review`, `SpecStore`.

## Execution Handoff
Commit this plan + design to `main`, then `mship spawn` MOS-166.
