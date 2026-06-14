# `mship serve` auth + `--host` — Implementation Plan (B2 / MOS-153)

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Make `mship serve` reachable beyond loopback (`--host`) and gate it with a `MSHIP_SERVE_TOKEN` bearer token, with a safety interlock that refuses to bind a non-loopback host without a token. Plus a Tailscale setup doc.

**Architecture:** `create_app(..., auth_token)` adds an app-level FastAPI auth dependency (constant-time bearer check) when a token is set. `cli/serve.py` gains `--host`, reads the token from env, runs the interlock before `uvicorn.run`. Design: [docs/superpowers/specs/2026-06-14-mship-serve-auth-design.md](../specs/2026-06-14-mship-serve-auth-design.md).

**Tech Stack:** FastAPI, uvicorn, Typer, pytest. Builds on B1 (`core/serve.py`, `cli/serve.py`, in `main`).

---

## File structure
- **Modify** `src/mship/core/serve.py` (`auth_token` param + auth dependency).
- **Modify** `src/mship/cli/serve.py` (`--host` + token + interlock).
- **Create** `docs/mship-serve-tailscale.md` (setup guide).
- **Test** `tests/core/test_serve.py` (auth), `tests/cli/test_serve.py` (interlock).

---

## Task 1: bearer-token auth in `create_app`

**Files:** `src/mship/core/serve.py`; `tests/core/test_serve.py`.

- [ ] **Step 1: Failing tests** (append to `tests/core/test_serve.py`):

```python
def _auth_app(tmp_path: Path, token: str | None):
    _seed_spec(tmp_path)  # from Task 2 of B1 — a spec to GET
    state = StateManager(tmp_path / ".mothership")
    return create_app(
        specs_dir=tmp_path / "specs", state_manager=state, log_manager=None,
        workspace_root=tmp_path, workspace_name="test-ws", auth_token=token,
    )


def test_auth_required_when_token_set(tmp_path):
    client = TestClient(_auth_app(tmp_path, "secret"))
    assert client.get("/specs").status_code == 401                       # no header
    assert client.get("/specs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/specs", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200


def test_open_when_no_token(tmp_path):
    # auth_token=None preserves B1 behavior (no auth).
    assert TestClient(_auth_app(tmp_path, None)).get("/specs").status_code == 200
```

- [ ] **Step 2: Run → fail** (`uv run pytest tests/core/test_serve.py -k auth -v` → `create_app` has no `auth_token`).

- [ ] **Step 3: Implement** — in `src/mship/core/serve.py`:
  - Add `auth_token: str | None = None` to `create_app`'s signature.
  - Add a module-level (or in-factory) dependency factory + wire it as an app-level dependency:

```python
def _make_auth_dependency(token: str):
    import hmac
    from fastapi import Header, HTTPException

    def _require_token(authorization: str | None = Header(default=None)):
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    return _require_token
```

  - Change the app construction (currently `app = FastAPI(title="mship serve", version="0")`) to:

```python
    from fastapi import Depends, FastAPI, HTTPException

    dependencies = [Depends(_make_auth_dependency(auth_token))] if auth_token else []
    app = FastAPI(title="mship serve", version="0", dependencies=dependencies)
```

  (App-level `dependencies` apply to every route — including `/health` and `/docs` — when a token is set. That's intended.)

- [ ] **Step 4: Run → pass.** Also run the whole `tests/core/test_serve.py` (the existing no-auth tests still pass since `_app()` passes no `auth_token`).

- [ ] **Step 5: Commit** `feat(serve): bearer-token auth on create_app (constant-time)` + `mship journal`.

---

## Task 2: `--host` + env token + safety interlock (CLI)

**Files:** `src/mship/cli/serve.py`; `tests/cli/test_serve.py`.

- [ ] **Step 1: Failing tests** (append to `tests/cli/test_serve.py`). Use the B1 `configured_app_with_task` fixture for the pass path; monkeypatch `uvicorn.run` to avoid blocking:

```python
def test_serve_refuses_nonloopback_without_token(monkeypatch):
    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code != 0
    assert "MSHIP_SERVE_TOKEN" in result.output


def test_serve_binds_nonloopback_with_token(configured_app_with_task, monkeypatch):
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "secret")
    called = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.setdefault("host", k.get("host")))
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 0, result.output
    assert called["host"] == "0.0.0.0"
```

  (`configured_app_with_task` is in `tests/cli/test_spec.py`; if it isn't importable from `test_serve.py`, replicate its container-override setup or move it to `conftest.py`. Simplest: add a minimal local fixture in `test_serve.py` that overrides `container.config_path`/`state_dir` like `configured_app_with_task` does.)

- [ ] **Step 2: Run → fail** (no `--host`).

- [ ] **Step 3: Implement** — rewrite the `serve` command in `src/mship/cli/serve.py`:

```python
    @app.command()
    def serve(
        host: str = typer.Option(
            "127.0.0.1", "--host",
            help="Bind address. Use your tailnet IP (or 0.0.0.0) to reach it from "
                 "other devices — requires MSHIP_SERVE_TOKEN.",
        ),
        port: int = typer.Option(47100, "--port", help="Port."),
    ):
        """Run a read-only JSON API over the spec + task model (Ground Control)."""
        import os
        import uvicorn
        from mship.core.serve import create_app
        from mship.core.spec_store import SPECS_DIRNAME

        output = Output()
        token = os.environ.get("MSHIP_SERVE_TOKEN")
        loopback = {"127.0.0.1", "localhost", "::1"}
        if host not in loopback and not token:
            output.error(
                f"Refusing to bind to non-loopback host {host!r} without auth. "
                f"Set MSHIP_SERVE_TOKEN to expose the API safely."
            )
            raise typer.Exit(1)

        container = get_container()
        workspace_root = Path(container.config_path()).parent
        api = create_app(
            specs_dir=workspace_root / SPECS_DIRNAME,
            state_manager=container.state_manager(),
            log_manager=container.log_manager(),
            workspace_root=workspace_root,
            workspace_name=container.config().workspace,
            auth_token=token,
        )
        auth_note = "auth: bearer token" if token else "auth: none (loopback only)"
        output.print(f"mship serve → http://{host}:{port}  ({auth_note}; docs: /docs)")
        uvicorn.run(api, host=host, port=port)
```

  (Interlock runs BEFORE `get_container`, so the refuse-path test doesn't need a configured workspace.)

- [ ] **Step 4: Run → pass.** Run the whole `tests/cli/test_serve.py` (the B1 `serve --help` smoke still passes).

- [ ] **Step 5: Commit** `feat(serve): --host + MSHIP_SERVE_TOKEN + non-loopback safety interlock` + journal.

---

## Task 3: Tailscale setup doc + full suite

**Files:** `docs/mship-serve-tailscale.md`; (verification).

- [ ] **Step 1: Write the doc** `docs/mship-serve-tailscale.md` — concise setup guide:
  - **Goal:** reach `mship serve` from your phone over Tailscale.
  - **Steps:** (1) `tailscale up` on the host (+ the phone); (2) get the host's tailnet IP (`tailscale ip -4`); (3) `export MSHIP_SERVE_TOKEN=$(openssl rand -hex 32)`; (4) `mship serve --host <tailnet-ip>`; (5) from the phone, hit `http://<tailnet-ip>:47100/specs` with header `Authorization: Bearer $MSHIP_SERVE_TOKEN`.
  - **Security notes:** the interlock refuses non-loopback binding without the token; the token is read from env, never committed; everything (incl. `/health`, `/docs`) requires the token when set; `tailscale serve` (TLS-terminating proxy to `127.0.0.1:47100`) is a more locked-down alternative.
  - **Verify locally:** `MSHIP_SERVE_TOKEN=secret mship serve --host 127.0.0.1` then `curl -H "Authorization: Bearer secret" localhost:47100/health`.

- [ ] **Step 2: Full suite.** `mship test` (or `uv run pytest`) → green.

- [ ] **Step 3: Commit** `docs(serve): Tailscale + auth setup guide` + journal.

---

## Self-Review
- **Coverage:** token auth (T1) + 401/200/wrong/None-open tests; `--host` + interlock (T2) + refuse/pass tests; doc + full suite (T3).
- **Placeholders:** none — complete code; the one fixture caveat (reuse vs. replicate `configured_app_with_task`) is called out.
- **Security:** constant-time compare; interlock prevents open non-loopback exposure; token from env not config; auth covers all routes when set.
- **Compat:** default `--host 127.0.0.1` + no token = exactly B1.
- **Type consistency:** `create_app(..., auth_token)`, `_make_auth_dependency(token)`; reuses the B1 app + `_seed_spec`/`configured_app_with_task` test helpers.

## Execution Handoff
Commit this plan + design to `main`, then `mship spawn` MOS-153.
