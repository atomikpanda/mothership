# Relay Caddy Front Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `relay-caddy-front` (approved) — `specs/2026-06-26-relay-caddy-front.md`

**Goal:** Put the relay's device-enrollment endpoint behind the relay's existing 443 via a Caddy reverse proxy with on-demand TLS, so there's no side port / firewall hole, certs issue only for relay-owned hosts, the enroll surface is edge-hardened, and the operator supplies only the relay host.

**Architecture:** Caddy becomes the sole public web ingress (80/443, `network_mode: host`); sish moves behind it (`--https=false`, internal `127.0.0.1:8080`, keeps 2222 for SSH tunnels). Caddy host-routes `enroll.<relay>` → the enroll-server (now `127.0.0.1:47180`) and `*.<relay>` → sish. TLS is Caddy on-demand gated by an `ask` endpoint backed by a pure predicate `tls_ask_allowed(domain, relay_domain)` that allows only `enroll.<relay>` and `<slug>-<6hex>.<relay>` serve subdomains. The enroll-server gains the ask route and binds loopback; the `mship relay enroll` CLI derives `https://enroll.<relay-host>` from `--relay-host`.

**Tech Stack:** Python (FastAPI, typer, pytest), Caddy 2, docker-compose, sish.

---

<!-- mship:task id=1 -->
### Task 1: `tls_ask_allowed` — the cert allowlist predicate (pure core)

The security-critical heart: Caddy will only provision a TLS cert for a hostname this returns `True` for. It must allow exactly `enroll.<relay_domain>` and the serve per-device shape `<base>-<6hex>.<relay_domain>` (see `device_subdomain` in `core/relay/tunnel.py`: base is `[a-z0-9-]`, id is `[0-9a-f]{6}`, whole label ≤63), and reject everything else — the bare apex, foreign domains, extra subdomain levels, lookalikes, and whitespace/empty input.

**Files:**
- Create: `src/mship/core/relay/tls_ask.py`
- Test: `tests/core/relay/test_tls_ask.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/core/relay/test_tls_ask.py
from mship.core.relay.tls_ask import tls_ask_allowed

RELAY = "mship-relay.atomikpanda.com"

def test_allows_enroll_host():
    assert tls_ask_allowed(f"enroll.{RELAY}", RELAY) is True

def test_allows_serve_subdomains():
    # device_subdomain output: <slug>-<6hex>
    assert tls_ask_allowed(f"mship-workspace-92bbb7.{RELAY}", RELAY) is True
    assert tls_ask_allowed(f"x-000000.{RELAY}", RELAY) is True

def test_rejects_bare_apex():
    assert tls_ask_allowed(RELAY, RELAY) is False

def test_rejects_foreign_domain():
    assert tls_ask_allowed("example.com", RELAY) is False
    assert tls_ask_allowed(f"mship-workspace-92bbb7.evil.com", RELAY) is False

def test_rejects_lookalike_and_extra_levels():
    assert tls_ask_allowed(f"enroll.{RELAY}.evil.com", RELAY) is False
    assert tls_ask_allowed(f"a.enroll.{RELAY}", RELAY) is False          # extra level
    assert tls_ask_allowed(f"x.mship-workspace-92bbb7.{RELAY}", RELAY) is False

def test_rejects_non_serve_label():
    assert tls_ask_allowed(f"random.{RELAY}", RELAY) is False            # no -<6hex>
    assert tls_ask_allowed(f"foo-92bbbz.{RELAY}", RELAY) is False        # z not hex

def test_rejects_blank_and_whitespace():
    assert tls_ask_allowed("", RELAY) is False
    assert tls_ask_allowed("   ", RELAY) is False
    assert tls_ask_allowed(f"enroll.{RELAY}", "") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/core/relay/test_tls_ask.py -q`
Expected: FAIL — `ModuleNotFoundError: mship.core.relay.tls_ask`

- [ ] **Step 3: Write the implementation**

```python
# src/mship/core/relay/tls_ask.py
from __future__ import annotations
import re

# A serve per-device subdomain LABEL: <base>-<6 hex>, where base is the
# DNS-safe workspace slug ([a-z0-9-]). Mirrors device_subdomain() in tunnel.py.
_SERVE_LABEL = re.compile(r"[a-z0-9-]+-[0-9a-f]{6}")


def tls_ask_allowed(domain: str, relay_domain: str) -> bool:
    """Whether Caddy may provision an on-demand TLS cert for `domain`.

    True only for the enroll host and serve per-device subdomains under
    `relay_domain`; False for the bare apex, foreign domains, extra subdomain
    levels, lookalikes, and blank input. This is the cert allowlist — keep it
    tight; a loose match reopens the "mint a cert for any host" surface.
    """
    domain = (domain or "").strip().lower()
    relay_domain = (relay_domain or "").strip().lower()
    if not domain or not relay_domain:
        return False
    suffix = "." + relay_domain
    if not domain.endswith(suffix):
        return False
    label = domain[: -len(suffix)]
    if not label or "." in label:        # no nested subdomain levels
        return False
    if label == "enroll":
        return True
    return len(label) <= 63 and _SERVE_LABEL.fullmatch(label) is not None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/core/relay/test_tls_ask.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit (pair with `mship journal`)**

```bash
git add src/mship/core/relay/tls_ask.py tests/core/relay/test_tls_ask.py
git commit -m "relay: add tls_ask_allowed cert allowlist predicate"
mship journal "tls_ask_allowed predicate + allow/deny matrix tests" --action committed --test-state pass
```
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2: Mount the on-demand `ask` route on the enroll app

Caddy's on-demand TLS calls `GET /tls-check?domain=<sni>` over loopback and issues a cert only on a 2xx. Add that route to the enroll app, backed by `tls_ask_allowed`. The app now needs to know the relay domain, so thread it through `build_enroll_app`.

**Files:**
- Modify: `src/mship/core/relay/enroll_app.py`
- Test: `tests/core/relay/test_enroll_app.py` (add cases)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/core/relay/test_enroll_app.py
from fastapi.testclient import TestClient
from mship.core.relay.enroll import RequestStore
from mship.core.relay.enroll_app import build_enroll_app

RELAY = "mship-relay.atomikpanda.com"

def _client(tmp_path):
    return TestClient(build_enroll_app(RequestStore(tmp_path / "store"), relay_domain=RELAY))

def test_tls_check_allows_relay_owned_host(tmp_path):
    c = _client(tmp_path)
    assert c.get("/tls-check", params={"domain": f"enroll.{RELAY}"}).status_code == 200
    assert c.get("/tls-check", params={"domain": f"w-92bbb7.{RELAY}"}).status_code == 200

def test_tls_check_rejects_foreign_host(tmp_path):
    c = _client(tmp_path)
    assert c.get("/tls-check", params={"domain": "evil.com"}).status_code == 403

def test_tls_check_requires_domain(tmp_path):
    c = _client(tmp_path)
    assert c.get("/tls-check").status_code in (400, 422)
```

(If existing tests call `build_enroll_app(store)` with no `relay_domain`, update them to pass `relay_domain=RELAY` — keep the param keyword-only with no default so the wiring is explicit.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/core/relay/test_enroll_app.py -q`
Expected: FAIL — `build_enroll_app() got an unexpected keyword argument 'relay_domain'` / 404 on `/tls-check`

- [ ] **Step 3: Write the implementation**

```python
# src/mship/core/relay/enroll_app.py  — change the builder signature + add the route
from fastapi import FastAPI, HTTPException, Query, Response
from mship.core.relay.tls_ask import tls_ask_allowed
# ... existing imports ...

def build_enroll_app(store: RequestStore, *, relay_domain: str) -> FastAPI:
    app = FastAPI(title="mship relay enroll")

    # ... existing /enroll and /status routes unchanged ...

    @app.get("/tls-check")
    def tls_check(domain: str = Query(..., max_length=253)):
        # Caddy on-demand TLS ask endpoint: 2xx => issue a cert for `domain`.
        if not tls_ask_allowed(domain, relay_domain):
            raise HTTPException(status_code=403, detail="host not allowed")
        return Response(status_code=200)

    return app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/core/relay/test_enroll_app.py -q`
Expected: PASS (new + existing, after updating existing calls to pass `relay_domain`)

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/relay/enroll_app.py tests/core/relay/test_enroll_app.py
git commit -m "relay: enroll app serves Caddy on-demand /tls-check ask route"
mship journal "added /tls-check ask route; build_enroll_app takes relay_domain" --action committed --test-state pass
```
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3: enroll-server binds loopback + takes `--relay-domain`

The server is now reached only via Caddy on loopback, so it should bind `127.0.0.1` (not `0.0.0.0`) — that's what actually closes the firewall hole. It also needs the relay domain to answer `/tls-check`.

**Files:**
- Modify: `src/mship/cli/relay.py` (the `enroll-server` command)
- Test: `tests/cli/test_relay_enroll_server.py` (create, or add to the existing relay CLI test module)

- [ ] **Step 1: Write the failing test**

The launch is `uvicorn.run(...)`; assert wiring by monkeypatching `uvicorn.run` and `build_enroll_app` and invoking the command via typer's `CliRunner`.

```python
# tests/cli/test_relay_enroll_server.py
import mship.cli.relay as relay_mod

def test_enroll_server_binds_loopback_and_passes_relay_domain(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(relay_mod, "_run_uvicorn", lambda app, host, port: calls.update(host=host, port=port))
    captured = {}
    import mship.core.relay.enroll_app as ea
    monkeypatch.setattr(ea, "build_enroll_app",
                        lambda store, *, relay_domain: captured.update(relay_domain=relay_domain) or "APP")
    relay_mod._enroll_server_impl(  # pure helper the command delegates to
        store_dir=str(tmp_path / "s"), pubkeys_dir=str(tmp_path / "p"),
        port=47180, host="127.0.0.1", ttl=1800, relay_domain="r.example.com",
    )
    assert calls["host"] == "127.0.0.1"
    assert captured["relay_domain"] == "r.example.com"
```

(Refactor note: extract the command body into a testable `_enroll_server_impl(...)` and a thin `_run_uvicorn(app, host, port)` seam so the test never starts a server. The `@relay_app.command("enroll-server")` function just parses options and calls `_enroll_server_impl`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/cli/test_relay_enroll_server.py -q`
Expected: FAIL — `_enroll_server_impl` / `_run_uvicorn` don't exist yet

- [ ] **Step 3: Write the implementation**

```python
# src/mship/cli/relay.py
import os

def _run_uvicorn(app, host, port):   # seam so tests don't boot a server
    import uvicorn
    uvicorn.run(app, host=host, port=port)

def _enroll_server_impl(*, store_dir, pubkeys_dir, port, host, ttl, relay_domain):
    from pathlib import Path
    from mship.core.relay.enroll import RequestStore
    from mship.core.relay.enroll_app import build_enroll_app
    if not relay_domain:
        raise typer.BadParameter("relay domain required: pass --relay-domain or set RELAY_DOMAIN")
    store = RequestStore(Path(store_dir), ttl_seconds=ttl)
    Output().print(f"enroll-server → http://{host}:{port}  (relay: {relay_domain}, "
                   f"pubkeys: {pubkeys_dir}, store: {store_dir}, ttl: {ttl}s)")
    _run_uvicorn(build_enroll_app(store, relay_domain=relay_domain), host=host, port=port)

@relay_app.command("enroll-server")
def enroll_server(
    pubkeys_dir: str = typer.Option("./pubkeys", "--pubkeys-dir", help="..."),
    store_dir: str = typer.Option("./pending-store", "--store-dir", help="..."),
    port: int = typer.Option(47180, "--port", help="Port to listen on."),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind (loopback; Caddy fronts it)."),
    ttl: int = typer.Option(1800, "--ttl", help="Pending request TTL in seconds."),
    relay_domain: str = typer.Option(lambda: os.environ.get("RELAY_DOMAIN", ""), "--relay-domain",
                                     help="Relay domain for the on-demand TLS ask (default $RELAY_DOMAIN)."),
):
    """Run the enroll endpoint on the relay host (behind Caddy; loopback by default)."""
    _enroll_server_impl(store_dir=store_dir, pubkeys_dir=pubkeys_dir, port=port,
                        host=host, ttl=ttl, relay_domain=relay_domain)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/cli/test_relay_enroll_server.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/relay.py tests/cli/test_relay_enroll_server.py
git commit -m "relay: enroll-server binds loopback + takes --relay-domain for the ask route"
mship journal "enroll-server 127.0.0.1 default + --relay-domain wired to build_enroll_app" --action committed --test-state pass
```
<!-- /mship:task -->

<!-- mship:task id=4 -->
### Task 4: `mship relay enroll` derives `https://enroll.<relay-host>`

The device side: operator passes only the relay host. Encode the URL precedence in a pure helper so it's testable without httpx.

**Files:**
- Modify: `src/mship/cli/relay.py` (the `enroll` command + a new pure helper)
- Test: `tests/cli/test_relay_enroll_url.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/cli/test_relay_enroll_url.py
import pytest
from mship.cli.relay import enroll_base_url

def test_explicit_enroll_url_wins():
    assert enroll_base_url(enroll_url="http://h:47180", relay_host="r.example.com",
                           config_host="c.example.com") == "http://h:47180"

def test_relay_host_derives_https_enroll_subdomain():
    assert enroll_base_url(enroll_url=None, relay_host="r.example.com",
                           config_host=None) == "https://enroll.r.example.com"

def test_falls_back_to_config_host():
    assert enroll_base_url(enroll_url=None, relay_host=None,
                           config_host="c.example.com") == "https://enroll.c.example.com"

def test_errors_when_nothing_given():
    with pytest.raises(ValueError):
        enroll_base_url(enroll_url=None, relay_host=None, config_host=None)

def test_strips_trailing_slash_on_override():
    assert enroll_base_url(enroll_url="http://h:47180/", relay_host=None,
                           config_host=None) == "http://h:47180"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/cli/test_relay_enroll_url.py -q`
Expected: FAIL — `cannot import name 'enroll_base_url'`

- [ ] **Step 3: Write the implementation**

```python
# src/mship/cli/relay.py
def enroll_base_url(*, enroll_url: str | None, relay_host: str | None,
                    config_host: str | None) -> str:
    """Resolve the enroll endpoint base URL. Precedence:
    explicit --enroll-url  >  --relay-host  >  configured relay.host.
    A relay host derives https://enroll.<host>."""
    if enroll_url:
        return enroll_url.rstrip("/")
    host = relay_host or config_host
    if not host:
        raise ValueError("provide --relay-host (or configure relay.host)")
    return f"https://enroll.{host.strip().rstrip('.')}"
```

Then change the `enroll` command to use it. `--enroll-url` becomes optional; add `--relay-host`; read the configured host best-effort from the container/config (e.g. `RelayConfig.from_mapping(...)` if a `relay:` block is present, else `None`):

```python
@relay_app.command("enroll")
def enroll_cmd(
    relay_host: str = typer.Option(None, "--relay-host",
                                   help="Relay host, e.g. mship-relay.example.com (enroll URL is derived)."),
    enroll_url: str = typer.Option(None, "--enroll-url",
                                   help="Explicit enroll base URL (overrides --relay-host)."),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="..."),
):
    out = Output()
    try:
        base = enroll_base_url(enroll_url=enroll_url, relay_host=relay_host,
                               config_host=_configured_relay_host(get_container))
    except ValueError as e:
        out.error(str(e)); raise typer.Exit(2)
    # ... existing httpx POST {base}/enroll + poll loop unchanged ...
```

Add a small `_configured_relay_host(get_container) -> str | None` that returns the workspace's `relay.host` if configured, else `None` (wrap in try/except so a non-workspace device just gets `None`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/cli/test_relay_enroll_url.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/relay.py tests/cli/test_relay_enroll_url.py
git commit -m "relay: enroll derives https://enroll.<relay-host>; --enroll-url now optional override"
mship journal "enroll_base_url precedence helper + --relay-host UX" --action committed --test-state pass
```
<!-- /mship:task -->

<!-- mship:task id=5 -->
### Task 5: Caddy front — Caddyfile + docker-compose

Config (no unit test). Caddy owns 80/443 (`network_mode: host`), gates on-demand TLS via the ask endpoint, hardens the enroll route, and proxies everything else to sish. sish stops terminating TLS and publishes HTTP only on loopback.

**Files:**
- Create: `docker/relay/Caddyfile`
- Modify: `docker/relay/docker-compose.yml`

- [ ] **Step 1: Write `docker/relay/Caddyfile`**

```
{
    on_demand_tls {
        ask http://127.0.0.1:47180/tls-check
    }
    email {$ACME_EMAIL}
}

# Enrollment — public but edge-hardened (only POST /enroll, GET /status/*).
enroll.{$RELAY_DOMAIN} {
    tls { on_demand }
    @enroll  { method POST  path /enroll }
    @status  { method GET   path /status/* }
    handle @enroll {
        request_body { max_size 4KB }
        reverse_proxy 127.0.0.1:47180
    }
    handle @status {
        reverse_proxy 127.0.0.1:47180
    }
    handle { respond "not found" 404 }
}

# Per-device serve subdomains → sish (Host preserved). More-specific
# enroll.<domain> above takes precedence over this wildcard.
*.{$RELAY_DOMAIN} {
    tls { on_demand }
    reverse_proxy 127.0.0.1:8080 {
        header_up Host {host}
    }
}
```

- [ ] **Step 2: Edit `docker/relay/docker-compose.yml`**

sish service: drop `- "80:80"` and `- "443:443"`; add `- "127.0.0.1:8080:8080"`; keep `- "2222:2222"`. In `command`: replace `--http-address=:80` with `--http-address=:8080`, set `--https=false`, and remove the `--https-address`, `--https=true`, and all `--https-ondemand-certificate*` flags (Caddy does TLS now). Keep `--domain`, `--authentication*`, `--private-keys-directory`, `--bind-random-*`, `--idle-connection`.

Add the Caddy service:

```yaml
  caddy:
    image: caddy:2
    restart: unless-stopped
    network_mode: host          # owns host :80/:443; reaches enroll-server + sish on 127.0.0.1
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./caddy-data:/data
      - ./caddy-config:/config
    environment:
      - RELAY_DOMAIN=${RELAY_DOMAIN}
      - ACME_EMAIL=${ACME_EMAIL}
```

- [ ] **Step 3: Validate the compose + Caddyfile render**

Run:
```bash
cd docker/relay && RELAY_DOMAIN=example.com ACME_EMAIL=x@example.com docker compose config >/dev/null && echo "compose OK"
docker run --rm -v "$PWD/Caddyfile":/etc/caddy/Caddyfile:ro caddy:2 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```
Expected: `compose OK` and Caddy reports the config valid. (If docker isn't available in the build env, note it and defer to the manual smoke step in Task 6.)

- [ ] **Step 4: Commit**

```bash
git add docker/relay/Caddyfile docker/relay/docker-compose.yml
git commit -m "relay: front sish with Caddy (on-demand TLS ask, enroll behind 443, loopback enroll-server)"
mship journal "Caddyfile + compose: Caddy host-net front, sish internal :8080, enroll route hardened" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=6 -->
### Task 6: Docs + run the full suite

**Files:**
- Modify: `docs/relay-hosting.md`

- [ ] **Step 1: Update `docs/relay-hosting.md`**

Document: Caddy is the public web front (80/443); sish runs behind it on internal `:8080` with `--https=false` (keeps 2222); the device-facing enroll URL is now `https://enroll.<relay-domain>` (no port); `mship relay enroll --relay-host <host>` derives it; the enroll-server binds `127.0.0.1` and takes `--relay-domain`; on-demand TLS is gated by `/tls-check` so certs issue only for `enroll.<relay>` and serve subdomains. Add the manual smoke steps below. Note the deferred items: rate-limit plugin, wildcard DNS-01 as an alternative.

- [ ] **Step 2: Run the full suite**

Run: `mship test --repos mothership`
Expected: green; new tests (tls_ask matrix, /tls-check route, enroll-server wiring, enroll URL derivation) included.

- [ ] **Step 3: Commit**

```bash
git add docs/relay-hosting.md
git commit -m "docs: relay Caddy front + https://enroll.<host> enroll flow"
mship journal "docs updated; full suite green" --action committed --test-state pass
```

- [ ] **Manual smoke (out-of-band, on the relay host):** `docker compose up -d` (sish+caddy); `mship relay enroll-server --relay-domain <relay> --store-dir <abs> --pubkeys-dir <abs>` (loopback); from another device `mship relay enroll --relay-host <relay>` reaches `https://enroll.<relay>`; owner `mship relay approve <id>`; an existing `mship serve --relay` subdomain still loads through Caddy; port 47180 is not reachable from outside.
<!-- /mship:task -->

---

## Self-Review

- **Spec coverage:** ac1 → Task 5 (Caddy service + sish internal). ac2 → Task 5 (host routing). ac3 → Task 1 (predicate) + Task 2 (ask route). ac4 → Task 5 (Caddyfile method/path + body cap). ac5 → Task 3 (loopback bind) + Task 5. ac6 → Task 4 (CLI derivation). ac7 → Task 6 (suite + docs) and the per-task tests. All seven covered.
- **Deployment note:** `network_mode: host` for Caddy is what lets a loopback-only enroll-server (ac5) stay reachable; sish publishes HTTP on `127.0.0.1:8080` so Caddy reaches it on loopback. This is the topology that satisfies "no public 47180" without containerizing the enroll-server.
- **Ordering:** 1→2 (predicate before the route that uses it), 3 independent, 4 independent, 5 after 2/3 (Caddy points at the ask route + loopback server), 6 last.
