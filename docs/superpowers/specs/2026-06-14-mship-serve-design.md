# `mship serve` — read-only JSON API — design (B1 / MOS-152)

> Status: design approved 2026-06-14. Feeds `writing-plans`.
> Part of **Ground Control** (epic MOS-144). The persistent surface the GC app
> reads from. Builds on the A1–A7 spec lifecycle (all merged).

## Context

Ground Control needs a persistent endpoint a phone client can read. Today only an
*ephemeral* `mship view spec --web` server exists (stdlib `http.server`, one spec
at a time). B1 is a long-lived JSON API over the whole spec + task model.

## Decisions

1. **Core-library-direct (NOT CLI-wrapping).** `serve` imports the core
   (`SpecStore`, `build_review`, `StateManager` / `build_task_index`, `LogManager`)
   and serializes models to JSON itself. No subprocess, no parsing CLI output.
   **This removes the MOS-103 dependency** — the API shape is owned by `serve`, not
   by the CLI's TTY detection.
2. **FastAPI + uvicorn** (two new deps — deliberate). Reuses mship's pydantic v2
   models as response schemas, auto-generates OpenAPI/Swagger docs (a
   self-documenting contract for the GC app developer), gives clean path-param
   routing + validation, and sets up cleanly for the write endpoints (follow-on)
   and B2 auth (FastAPI dependencies/middleware). Route handlers are plain `def`
   (sync) — FastAPI runs them in a worker thread, so the sync core (`SpecStore`,
   `StateManager`) needs **no async rewrite**.
3. **Read-only (v0).** GET endpoints only. Writes (verdict / answer / approve /
   dispatch) are a tight follow-on (the core helpers already exist) — POST/PUT
   return `405` with a "writes not yet supported" body.
4. **Bind `127.0.0.1`, no auth.** B2 (MOS-153) adds Tailscale reachability +
   single-user auth. B1 is localhost-only.
5. **Testable via FastAPI `TestClient`.** Tests exercise the app with Starlette's
   `TestClient` (httpx-based, no real socket) — full request/response coverage
   without binding a port.

## Architecture

- **`src/mship/core/serve.py`** — `create_app(specs_dir, state_manager, log_manager,
  workspace_root) -> FastAPI`: builds the app and registers the GET routes; each
  handler calls the core and returns a pydantic model / dict (FastAPI serializes +
  schemas it). Workspace context is captured via the factory closure. This factory
  is the unit-tested surface (`TestClient(create_app(...))`).
- **`src/mship/cli/serve.py`** — the `mship serve [--port]` command: build the app
  from the container, then `uvicorn.run(app, host="127.0.0.1", port=port)`.
  Registered in `cli/__init__.py`. `fastapi`/`uvicorn` are imported **inside** the
  command (lazy) so the rest of the CLI doesn't pay their import cost.

## Endpoints (v0, all GET → JSON)

| Path | Returns |
|------|---------|
| `GET /health` | `{"status":"ok","workspace":<name>}` |
| `GET /specs` | list of `{id, title, status, task_slug}` (from `SpecStore.list()`) |
| `GET /specs/<id>` | the full spec (`spec.model_dump(mode="json")`), `404` if absent |
| `GET /specs/<id>/review` | `build_review(spec)` (the C4 review-card payload), `404` if absent |
| `GET /tasks` | `build_task_index(state, workspace_root)` summaries |
| `GET /tasks/<slug>` | that task's detail (status envelope), `404` if absent |
| `GET /journal/<slug>` | recent journal entries (`LogManager.read(slug)`), `404` if task absent |

- Any other path → `404 {"error": "..."}`. Non-GET → `405 {"error":"read-only; writes not yet supported"}`.
- Content-Type `application/json`; bodies are `json.dumps`'d.
- Spec id / task slug parsed from the path; unsafe/unknown → `404`.

## Scope (B1 v0)

In: the `SpecApi` router + the GET endpoints + the `mship serve` command + a default
port (`47100`, `--port` to override). Out: write endpoints (follow-on), auth +
Tailscale (B2), the app (C), MOS-103 (decoupled).

## Migration / compat

Additive — a new command + new modules; no change to existing behavior. Adds two
runtime dependencies (`fastapi`, `uvicorn`) to `pyproject.toml` — the first
web-framework deps in the repo.

## Testing

- `TestClient(create_app(...))` (no socket): `/health`; `/specs` lists seeded specs;
  `/specs/<id>` → spec / `404` for unknown; `/specs/<id>/review` → a
  `build_review`-shaped payload; `/tasks` + `/tasks/<slug>` (seeded state) + `404`;
  `/journal/<slug>`; unknown path → `404`; `POST /specs/<id>/...` → `405` (no write
  route registered). `TestClient` exercises the real ASGI app end-to-end, so no
  separate socket test is needed.

## Follow-ons

- **Writes** (verdict / answer / approve / request-changes / dispatch) over POST —
  thin wrappers on the existing core helpers; the next increment.
- **B2 (MOS-153)** — Tailscale bind + single-user auth on top of this server.
- **MOS-103** — still worth doing for CLI/CI determinism, but no longer blocks B1.
