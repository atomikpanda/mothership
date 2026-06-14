# Reaching `mship serve` from your phone (Tailscale)

`mship serve` exposes a read-only JSON API over your specs + tasks. By default it
binds to `127.0.0.1` with no auth. To reach it from another device, bind to your
tailnet IP and set a bearer token.

## One-time setup

1. Install + start Tailscale on the host and the phone, on the same tailnet:
   ```
   tailscale up
   ```
2. Find the host's tailnet IP:
   ```
   tailscale ip -4   # e.g. 100.x.y.z
   ```

## Run the server

```bash
export MSHIP_SERVE_TOKEN="$(openssl rand -hex 32)"
mship serve --host <tailnet-ip>      # e.g. --host 100.x.y.z  (or 0.0.0.0)
```

`mship serve` **refuses to bind a non-loopback host without `MSHIP_SERVE_TOKEN`** —
this prevents accidentally exposing your specs unauthenticated. The exact error:

```
Error: Refusing to bind to non-loopback host '100.x.y.z' without auth.
       Set MSHIP_SERVE_TOKEN to expose the API safely.
```

Default port is `47100`. Override with `--port`.

## Call it from the phone

```
GET http://<tailnet-ip>:47100/specs
Authorization: Bearer <your MSHIP_SERVE_TOKEN>
```

Every endpoint requires the token; a missing or wrong token returns `401 Unauthorized`.

Available endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Server health + workspace name |
| GET | `/specs` | List all specs (id, title, status, task_slug) |
| GET | `/specs/{id}` | Full spec detail |
| GET | `/specs/{id}/review` | Spec review data |
| GET | `/tasks` | List all tasks |
| GET | `/tasks/{slug}` | Single task detail |
| GET | `/journal/{slug}` | Last 50 journal entries for a task |

## Interactive docs

When a token is set, the interactive docs (`/docs`, `/redoc`, `/openapi.json`) are
disabled — no unauthenticated schema surface is exposed. To browse `/docs` during
development, run a local `mship serve` without a token (loopback only):

```bash
mship serve   # binds 127.0.0.1:47100, no token required, /docs available
```

## Security notes

- The token is read from the environment at startup; it is never written to
  `mothership.yaml` or any on-disk state file.
- Only your tailnet peers can route to the tailnet IP. The bearer token is a second
  layer on top of Tailscale's network-level access control.
- More locked-down alternative: keep `mship serve` on `127.0.0.1` and front it with
  Tailscale's HTTPS proxy, which terminates TLS and restricts access to tailnet peers:
  ```
  tailscale serve https / http://127.0.0.1:47100
  ```

## Verify locally

```bash
# Start with auth on loopback
MSHIP_SERVE_TOKEN=secret mship serve --host 127.0.0.1 &

# Authenticated request — should return {"status":"ok",...}
curl -s -H "Authorization: Bearer secret" http://127.0.0.1:47100/health

# Unauthenticated request — should return 401
curl -s http://127.0.0.1:47100/health

# Kill the background server when done
kill %1
```
