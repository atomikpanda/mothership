# `mship serve` auth + tailnet reachability — design (B2 / MOS-153)

> Status: design approved 2026-06-14. Feeds `writing-plans`.
> Part of **Ground Control** (epic MOS-144). Adds the auth + reachability layer on
> top of B1's read-only `mship serve` (merged, #170).

## Context

B1 serves the spec/task API on `127.0.0.1` with no auth (localhost = trusted,
single-user). B2 makes it reachable from a phone over **Tailscale** and gates it
with a **bearer token** — so the API is safe once it leaves loopback. Tailscale
itself is the user's infra (a `tailscale up` device on the tailnet); mship provides
the bindable host + auth + setup docs.

## Decisions

1. **`mship serve --host <addr>`** — new flag (default `127.0.0.1`). Set it to the
   device's tailnet IP (or `0.0.0.0`) so tailnet peers can reach it directly.
2. **Static bearer token via env `MSHIP_SERVE_TOKEN`.** When set, every endpoint
   requires `Authorization: Bearer <token>` → `401` otherwise. Read from the env
   (never `mothership.yaml` — it's a secret). Transport-agnostic.
3. **🔒 Safety interlock (the backbone):** `mship serve` **refuses to start** when
   binding to a **non-loopback** host (`--host` not in `{127.0.0.1, localhost, ::1}`)
   **without** `MSHIP_SERVE_TOKEN` set — exits non-zero with a clear message. This
   makes accidental open exposure of the spec/task data impossible.
4. **Auth applies to *all* endpoints (incl. `/health`) when a token is set.**
   Simplest + most secure; the single-user app holds the token anyway. (No
   unauthenticated liveness probe in v0 — add one later if needed.)
5. **Localhost + no token stays open** — preserves B1 behavior (loopback dev use).
6. **Tailscale = documented, not automated.** A setup doc covers `tailscale up`,
   finding the tailnet IP, `mship serve --host <tailnet-ip>`, and setting the token.
   (`tailscale serve` proxy is mentioned as an alternative but `--host` is the
   chosen path.)

## Architecture

- **`core/serve.py`** — `create_app(..., auth_token: str | None = None)`. When
  `auth_token` is set, the app gets an **app-level dependency** that checks the
  `Authorization` header against `Bearer <auth_token>` and raises `401` on
  mismatch; when `None`, no dependency (open, B1 behavior). A small
  `_make_auth_dependency(token)` factory returns the dependency.
- **`cli/serve.py`** — add `--host` (default `127.0.0.1`); read
  `token = os.environ.get("MSHIP_SERVE_TOKEN")`; run the interlock (non-loopback +
  no token → `output.error` + `Exit(1)`); `create_app(..., auth_token=token)`;
  `uvicorn.run(api, host=host, port=port)`. Print whether auth is on.

## Auth contract

`Authorization: Bearer <MSHIP_SERVE_TOKEN>` on every request. Missing/wrong →
`401 {"detail": "..."}`. Constant-time compare (`hmac.compare_digest`) to avoid a
timing side-channel on the token.

## Testing

- **create_app auth** (`TestClient`): with `auth_token="secret"` — `GET /specs`
  with no header → `401`; with `Authorization: Bearer secret` → `200`; wrong token
  → `401`. With `auth_token=None` — open (`200` no header), proving B1 compat.
- **CLI interlock** (`CliRunner`): `serve --host 0.0.0.0` with `MSHIP_SERVE_TOKEN`
  unset → exits non-zero, message names the env var (this path returns *before*
  `uvicorn.run`, so no hang). With the token set + `uvicorn.run` monkeypatched to a
  no-op → exits `0` (interlock passes, app built with the token). Loopback default
  (no token) is not invoked via the runner (it would block on `uvicorn.run`).

## Scope (B2)

In: `--host`, the bearer-token auth dependency + the safety interlock, the
constant-time compare, and the Tailscale setup doc. Out: Tailscale identity-header
trust (follow-on), TLS termination in mship (Tailscale/`tailscale serve` owns TLS),
write endpoints (separate), multi-user auth.

## Migration / compat

Additive. Default `--host 127.0.0.1` + no token = exactly B1's behavior. The
interlock only triggers on an explicit non-loopback `--host`.
