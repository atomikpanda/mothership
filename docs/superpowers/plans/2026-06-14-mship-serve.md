# `mship serve` (read-only FastAPI) — Implementation Plan (B1 / MOS-152)

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** A persistent, read-only JSON API over the spec + task model for Ground Control, built with FastAPI, calling the mship core directly.

**Architecture:** `core/serve.py` exposes `create_app(specs_dir, state_manager, log_manager, workspace_root, workspace_name) -> FastAPI` with sync GET handlers that call the core (`SpecStore`, `build_review`, `build_task_index`, `LogManager`) — FastAPI serializes the returns. `cli/serve.py` runs it via `uvicorn` on `127.0.0.1`. Tested with FastAPI `TestClient` (no socket). Design: [docs/superpowers/specs/2026-06-14-mship-serve-design.md](../specs/2026-06-14-mship-serve-design.md).

**Tech Stack:** Python, FastAPI, uvicorn, pydantic v2, Typer, pytest. Reuses A1–A7 core (in `main`).

---

## File structure
- **Create** `src/mship/core/serve.py` (`create_app` factory).
- **Create** `src/mship/cli/serve.py` (`mship serve` command).
- **Modify** `pyproject.toml` (add `fastapi`, `uvicorn`), `uv.lock` (via `uv sync`), `src/mship/cli/__init__.py` (register).
- **Test** `tests/core/test_serve.py` (TestClient), `tests/cli/test_serve.py` (command smoke).

---

## Task 1: deps + app skeleton + `/health` + command wiring

**Files:** `pyproject.toml`, `src/mship/core/serve.py`, `src/mship/cli/serve.py`, `src/mship/cli/__init__.py`, `tests/core/test_serve.py`.

- [ ] **Step 1: Add deps + install.** In `pyproject.toml` `[project].dependencies`, add `"fastapi>=0.110"` and `"uvicorn>=0.27"`. Then run `uv sync` (updates `uv.lock` + the worktree venv) so `import fastapi` works under `uv run`. Verify: `uv run python -c "import fastapi, uvicorn; print('ok')"`.

- [ ] **Step 2: Failing test** (`tests/core/test_serve.py`):

```python
from pathlib import Path
from fastapi.testclient import TestClient

from mship.core.serve import create_app
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


def test_health(tmp_path):
    client = TestClient(_app(tmp_path))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "workspace": "test-ws"}
```

- [ ] **Step 3: Run → fail** (`uv run pytest tests/core/test_serve.py -v` → ImportError / no create_app).

- [ ] **Step 4: Implement** (`src/mship/core/serve.py`):

```python
from __future__ import annotations

from pathlib import Path


def create_app(
    specs_dir: Path,
    state_manager,
    log_manager,
    workspace_root: Path,
    workspace_name: str = "mothership",
):
    """Build the read-only mship serve FastAPI app. Sync handlers call the core
    directly; FastAPI serializes the returns (pydantic models, dicts, dataclasses)."""
    from fastapi import FastAPI, HTTPException

    from mship.core.spec_store import SpecStore

    app = FastAPI(title="mship serve", version="0")
    store = SpecStore(specs_dir)

    @app.get("/health")
    def health():
        return {"status": "ok", "workspace": workspace_name}

    return app
```

- [ ] **Step 5: Run → pass.** Then register the command. In `src/mship/cli/serve.py`:

```python
from __future__ import annotations

from pathlib import Path

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def serve(
        port: int = typer.Option(47100, "--port", help="Port to bind on 127.0.0.1."),
    ):
        """Run a read-only JSON API over the spec + task model (Ground Control)."""
        import uvicorn
        from mship.core.serve import create_app
        from mship.core.spec_store import SPECS_DIRNAME

        container = get_container()
        workspace_root = Path(container.config_path()).parent
        api = create_app(
            specs_dir=workspace_root / SPECS_DIRNAME,
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            workspace_root=workspace_root,
            workspace_name=container.config().workspace,
        )
        Output().print(f"mship serve → http://127.0.0.1:{port}  (docs: /docs)")
        uvicorn.run(api, host="127.0.0.1", port=port)
```

  In `src/mship/cli/__init__.py`: add `from mship.cli import serve as _serve_mod` (with the other imports) and `_serve_mod.register(app, get_container)` (with the other register calls).

- [ ] **Step 6: CLI smoke test** (`tests/cli/test_serve.py`):

```python
from typer.testing import CliRunner
from mship.cli import app

runner = CliRunner()


def test_serve_command_registered():
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "127.0.0.1" in result.output
```

- [ ] **Step 7: Commit** `feat(serve): mship serve skeleton (FastAPI app + /health + command)` + `mship journal`.

---

## Task 2: spec endpoints (`/specs`, `/specs/{id}`, `/specs/{id}/review`)

**Files:** `src/mship/core/serve.py`; `tests/core/test_serve.py`.

- [ ] **Step 1: Failing tests** (append; add a helper that seeds a spec via `SpecStore`):

```python
from datetime import datetime, timezone
from mship.core.spec import AcceptanceCriterion, Spec
from mship.core.spec_store import SpecStore
from mship.core.spec_body import render_body


def _seed_spec(tmp_path: Path):
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id="dq", title="Decision queue", status="needs_review",
        created_at=now, updated_at=now, task_slug="dq",
        body=render_body("the problem", "as a user", "the approach"),
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="view questions", verdict="approved")],
    ))


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
```

- [ ] **Step 2: Run → fail** (404s, routes not registered).

- [ ] **Step 3: Implement** — inside `create_app`, after `/health`:

```python
    from mship.core.spec_review import build_review

    @app.get("/specs")
    def list_specs():
        return [
            {"id": s.id, "title": s.title, "status": s.status, "task_slug": s.task_slug}
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
```

- [ ] **Step 4: Run → pass.** **Step 5: Commit** `feat(serve): /specs, /specs/{id}, /specs/{id}/review` + journal.

---

## Task 3: task + journal endpoints (`/tasks`, `/tasks/{slug}`, `/journal/{slug}`)

**Files:** `src/mship/core/serve.py`; `tests/core/test_serve.py`.

- [ ] **Step 1: Failing tests** (append; seed a task into state + a journal entry):

```python
from mship.core.state import Task, WorkspaceState
from mship.core.log import LogManager


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
```

- [ ] **Step 2: Run → fail.** **Step 3: Implement** — inside `create_app`:

```python
    from mship.core.view.task_index import build_task_index

    @app.get("/tasks")
    def list_tasks():
        return build_task_index(state_manager.load(), workspace_root)

    @app.get("/tasks/{slug}")
    def get_task(slug: str):
        state = state_manager.load()
        if slug not in state.tasks:
            raise HTTPException(status_code=404, detail=f"no task {slug!r}")
        by_slug = {t.slug: t for t in build_task_index(state, workspace_root)}
        return by_slug[slug]

    @app.get("/journal/{slug}")
    def get_journal(slug: str):
        state = state_manager.load()
        if slug not in state.tasks:
            raise HTTPException(status_code=404, detail=f"no task {slug!r}")
        return log_manager.read(slug, last=50)
```

  (FastAPI runs `jsonable_encoder` on returns, handling the `TaskSummary`/`LogEntry` dataclasses + their `Path`/`datetime` fields. If a field fails to encode, wrap the return in `from fastapi.encoders import jsonable_encoder` explicitly — but verify the plain return works first.)

- [ ] **Step 4: Run → pass.** **Step 5: Commit** `feat(serve): /tasks, /tasks/{slug}, /journal/{slug}` + journal.

---

## Task 4: read-only guard + full suite

**Files:** `tests/core/test_serve.py`; `tests/cli/test_serve.py`.

- [ ] **Step 1: Tests** — confirm read-only + unknown-path behavior (FastAPI defaults):

```python
def test_post_is_405(tmp_path):
    # No write routes registered → POST to a GET path is 405 (Method Not Allowed).
    r = TestClient(_app(tmp_path)).post("/specs/dq/review")
    assert r.status_code == 405


def test_unknown_path_404(tmp_path):
    assert TestClient(_app(tmp_path)).get("/nope").status_code == 404
```

- [ ] **Step 2: Run → pass.** If `POST /specs/dq/review` returns 404 rather than 405 (path templating nuance), adjust the test to POST a concrete GET path that exists for GET (e.g. `/health`) — confirm 405 there; the point is "no writes accepted."

- [ ] **Step 3: Full suite.** `mship test` (or `uv run pytest`) → green. Confirm the new `fastapi`/`uvicorn` deps didn't perturb anything; `uv.lock` is updated + committed.

- [ ] **Step 4: Commit** `test(serve): read-only guard + unknown-path; full suite green` + journal.

---

## Self-Review
- **Coverage:** `/health` (T1), `/specs` + `/specs/{id}` + `/review` (T2), `/tasks` + `/tasks/{slug}` + `/journal/{slug}` (T3), read-only 405 + 404 (T4). `create_app` factory + `mship serve` command + registration + deps (T1).
- **Placeholders:** none — complete code. The one runtime check (does the plain dataclass return serialize, else `jsonable_encoder`) is called out, not hidden.
- **Decisions honored:** core-direct (handlers call core, no CLI subprocess); FastAPI + uvicorn (deps added); read-only (no write routes); localhost bind; sync handlers.
- **Type consistency:** `create_app(specs_dir, state_manager, log_manager, workspace_root, workspace_name)`; reuses `SpecStore`, `build_review`, `build_task_index`, `LogManager.read`, `Spec.model_dump`, `SPECS_DIRNAME`.

## Execution Handoff
Commit this plan + design to `main`, then `mship spawn` MOS-152. Note T1 runs `uv sync` to install the new deps in the worktree before tests can import FastAPI.
