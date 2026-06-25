# Relay Multi-Device Identity + Loud Failures — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Origin:** debugging session 2026-06-25. Ground Control "can't connect via the relay" was a **stale pairing caused by a multi-device subdomain collision**: `subdomain_for(workspace)` is keyed on the workspace NAME only, but the bearer token is per-machine (`.mothership/serve-token`). Two devices serving the same workspace name request the same relay subdomain; whoever bound it while free wins, the other silently loses (sish honors-requested with `--bind-random-subdomains=false`, no `--load-balancer`), and `mship serve` advertises the *requested* URL regardless because it discards the ssh tunnel's output (`/dev/null`). Net: the QR can point at the wrong device, with the wrong token, and nothing surfaces why.

**Goal:** Make the relay path multi-device-safe and self-diagnosing: each device gets a **unique, stable subdomain**, and serve **verifies + reports** the tunnel instead of failing silently.

**Architecture:** Per-device subdomain = `<workspace-slug>-<short hash of this machine's relay public key>`. Capture the ssh tunnel's output instead of discarding it. After startup, a background thread verifies the public URL round-trips (`/health` + token) and prints `✓ reachable` / `✗ <reason>`; the tick loop warns loudly if the tunnel keeps respawning. No protocol/security-model change — token stays per-machine (now matched to a unique per-device URL).

**Tech Stack:** Python 3, pytest. `httpx` (already a dep) for the health probe. The pure pieces (subdomain, verifier) are unit-tested; the serve wiring is integration/smoke-verified.

---

## Files

| File | Change |
|---|---|
| `src/mship/core/relay/tunnel.py` | Add `device_id()` + `device_subdomain()`; capture ssh output (log file) in the default proc factory; `TunnelSupervisor` exposes `recent_output()` + `restart_count`. |
| `src/mship/core/relay/health.py` (new) | Pure `verify_relay_reachable(public_url, token, get=httpx.get)` → `(ok, detail)`. |
| `src/mship/cli/serve.py` | `_serve_with_relay`: per-device subdomain, capture log, background verify thread printing ✓/✗ (+ ssh output on failure), loud respawn warning. |
| `tests/core/relay/test_tunnel.py`, `tests/core/relay/test_health.py` | Unit tests (mirror existing relay test style). |

Run tests with `mship test --repos mothership` (or `uv run pytest -q`). Work in the worktree `.worktrees/relay-multi-device-identity-loud/mothership`, branch `feat/relay-multi-device-identity-loud`; never edit the main checkout.

---

<!-- mship:task id=1 -->
### Task 1: Per-device subdomain

**Files:**
- Modify: `src/mship/core/relay/tunnel.py`
- Test: `tests/core/relay/test_tunnel.py` (add to the existing file)

- [ ] **Step 1: Write failing tests**

```python
# add to tests/core/relay/test_tunnel.py
from mship.core.relay.tunnel import device_id, device_subdomain, subdomain_for

_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAA mship-relay\n"

def test_device_id_is_stable_short_hex():
    a = device_id(_PUBKEY)
    assert a == device_id(_PUBKEY)              # stable
    assert len(a) == 6 and all(c in "0123456789abcdef" for c in a)

def test_device_id_ignores_comment_and_whitespace():
    # same key body, different trailing comment/whitespace → same id
    assert device_id(_PUBKEY) == device_id("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAA other-comment")

def test_device_id_differs_per_key():
    assert device_id(_PUBKEY) != device_id("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDifferentBodyZZZZ x")

def test_device_subdomain_appends_id_and_is_dns_safe():
    sd = device_subdomain("mship-workspace", "abc123")
    assert sd == "mship-workspace-abc123"
    assert len(sd) <= 63 and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in sd)

def test_device_subdomain_truncates_to_dns_limit():
    sd = device_subdomain("w" * 80, "abc123")
    assert len(sd) <= 63
    assert sd.endswith("-abc123")
    assert not sd.startswith("-") and "--" not in sd
```

- [ ] **Step 2: Run → fail**

Run: `cd /home/bailey/development/repos/mship-workspace/.worktrees/relay-multi-device-identity-loud/mothership && uv run pytest tests/core/relay/test_tunnel.py -q -k "device" 2>&1 | tail -5`
Expected: FAIL — `device_id`/`device_subdomain` undefined.

- [ ] **Step 3: Implement (add to `tunnel.py`)**

```python
import hashlib  # add to the imports at the top

def device_id(relay_public_key: str) -> str:
    """Stable 6-char hex id for THIS machine, from its relay public key body.

    Uses only the base64 key material (the 2nd whitespace-delimited field),
    ignoring the trailing comment, so re-reading the key gives the same id.
    """
    parts = relay_public_key.split()
    body = parts[1] if len(parts) >= 2 else relay_public_key.strip()
    return hashlib.sha256(body.encode()).hexdigest()[:6]


def device_subdomain(workspace: str, device_id: str) -> str:
    """Per-device relay subdomain: `<workspace-slug>-<device_id>`, DNS-label-safe.

    The workspace slug is truncated so the whole label (slug + '-' + id) fits the
    63-char DNS limit, with any trailing '-' after truncation stripped.
    """
    suffix = f"-{device_id}"
    base = subdomain_for(workspace)[: 63 - len(suffix)].rstrip("-")
    return f"{base}{suffix}"
```

- [ ] **Step 4: Run → pass**

Run: `uv run pytest tests/core/relay/test_tunnel.py -q -k "device" 2>&1 | tail -5`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit + journal**

```bash
git add src/mship/core/relay/tunnel.py tests/core/relay/test_tunnel.py
git commit -m "feat(relay): per-device subdomain (device_id + device_subdomain)"
mship journal "relay: per-device subdomain; 5 tests" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2: Capture ssh tunnel output + surface it on the supervisor

**Files:**
- Modify: `src/mship/core/relay/tunnel.py`
- Test: `tests/core/relay/test_tunnel.py`

Stop discarding ssh stdout/stderr. The default proc factory writes them to a log file; `TunnelSupervisor` exposes `recent_output()` and `restart_count` so callers can show the real failure reason / assigned URL.

- [ ] **Step 1: Write failing tests**

```python
# add to tests/core/relay/test_tunnel.py
from pathlib import Path
from mship.core.relay.tunnel import TunnelSupervisor

class _FakeProc:
    def __init__(self, exits_after=0):
        self._polls = 0
        self._exits_after = exits_after
    def poll(self):
        self._polls += 1
        return None if self._polls <= self._exits_after else 1
    def terminate(self): pass
    def wait(self, timeout=None): return 0

def test_supervisor_exposes_recent_output(tmp_path):
    log = tmp_path / "tunnel.log"
    log.write_text("Warning: remote port forwarding failed for listen port 80\n")
    sup = TunnelSupervisor(argv=["ssh"], proc_factory=lambda a: _FakeProc(0), log_path=log)
    sup.start()
    assert "remote port forwarding failed" in sup.recent_output()

def test_supervisor_counts_restarts(tmp_path):
    # proc that is always "dead" → tick respawns once backoff elapses
    sup = TunnelSupervisor(argv=["ssh"], proc_factory=lambda a: _FakeProc(0),
                           backoff_delay=0.0, log_path=tmp_path / "t.log")
    sup.start()
    assert sup.restart_count == 0
    sup.tick(); sup.tick()
    assert sup.restart_count >= 1
```

- [ ] **Step 2: Run → fail**

Run: `uv run pytest tests/core/relay/test_tunnel.py -q -k "supervisor_exposes or counts_restarts" 2>&1 | tail -5`
Expected: FAIL — `log_path`/`recent_output`/`restart_count` not present.

- [ ] **Step 3: Implement**

In `tunnel.py`, change the default proc factory to accept a log path and redirect output there (keep `/dev/null` only when no log path is given, for existing callers/tests):

```python
def _default_proc_factory(argv: list[str], log_path: Path | None = None):
    """Launch argv in its own process group, capturing output to log_path
    (so failures/assigned-URL are inspectable). Falls back to DEVNULL."""
    if log_path is not None:
        out = open(log_path, "ab", buffering=0)
        kwargs: dict = dict(stdout=out, stderr=subprocess.STDOUT)
    else:
        kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)
```

Add `log_path` to `TunnelSupervisor.__init__` and expose helpers. Update the constructor signature to accept `log_path: Path | None = None`, store it, and have the default factory use it:

```python
    def __init__(self, argv, proc_factory=None, backoff_delay=5.0,
                 max_backoff_delay=60.0, clock=None, log_path=None):
        self._argv = argv
        self._log_path = log_path
        self._proc_factory = proc_factory if proc_factory is not None \
            else (lambda a: _default_proc_factory(a, self._log_path))
        # ... rest unchanged (backoff/clock/state) ...
```

Add the public accessors and expose `restart_count`:

```python
    @property
    def restart_count(self) -> int:
        return self._restart_count

    def recent_output(self, limit: int = 4000) -> str:
        """Tail of the captured ssh output (empty if no log/none yet)."""
        if self._log_path is None:
            return ""
        try:
            data = Path(self._log_path).read_bytes()[-limit:]
            return data.decode(errors="replace")
        except FileNotFoundError:
            return ""
```

(Keep the existing `_restart_count` field; the property just exposes it. Existing tests that pass no `log_path`/`proc_factory` still work.)

- [ ] **Step 4: Run → pass + the whole tunnel test file**

Run: `uv run pytest tests/core/relay/test_tunnel.py -q 2>&1 | tail -5`
Expected: PASS (new + all existing tunnel tests).

- [ ] **Step 5: Commit + journal**

```bash
git add src/mship/core/relay/tunnel.py tests/core/relay/test_tunnel.py
git commit -m "feat(relay): capture ssh tunnel output; expose recent_output + restart_count"
mship journal "relay: capture ssh output + supervisor accessors" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3: Pure relay reachability verifier

**Files:**
- Create: `src/mship/core/relay/health.py`
- Test: `tests/core/relay/test_health.py` (new)

A pure function the serve can call to confirm the public URL round-trips with the token — injectable HTTP for testing.

- [ ] **Step 1: Write failing tests**

```python
# tests/core/relay/test_health.py
from mship.core.relay.health import verify_relay_reachable

class _Resp:
    def __init__(self, status): self.status_code = status

def test_ok_when_authed_request_succeeds():
    calls = {}
    def get(url, headers=None, timeout=None, follow_redirects=None):
        calls["url"] = url; calls["auth"] = headers.get("Authorization")
        return _Resp(200)
    ok, detail = verify_relay_reachable("https://w-ab12.relay", "tok", get=get)
    assert ok is True
    assert calls["url"] == "https://w-ab12.relay/health"
    assert calls["auth"] == "Bearer tok"

def test_not_ok_on_401_explains_token():
    ok, detail = verify_relay_reachable("https://w.relay", "tok", get=lambda *a, **k: _Resp(401))
    assert ok is False and "token" in detail.lower()

def test_not_ok_on_exception_carries_reason():
    def boom(*a, **k): raise RuntimeError("name resolution failed")
    ok, detail = verify_relay_reachable("https://w.relay", "tok", get=boom)
    assert ok is False and "name resolution failed" in detail
```

- [ ] **Step 2: Run → fail**

Run: `uv run pytest tests/core/relay/test_health.py -q 2>&1 | tail -5`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# src/mship/core/relay/health.py
from __future__ import annotations
from typing import Callable


def verify_relay_reachable(public_url: str, token: str, *, get: Callable | None = None,
                           timeout: float = 8.0) -> tuple[bool, str]:
    """Probe `<public_url>/health` with the bearer token through the relay.

    Returns (ok, detail). ok=True only on 2xx. 401/403 → a token-mismatch hint.
    Any transport error → ok=False with the exception text (the real reason).
    `get` is injectable (defaults to httpx.get) for testing.
    """
    if get is None:
        import httpx
        get = lambda url, **kw: httpx.get(url, **kw)
    url = public_url.rstrip("/") + "/health"
    try:
        r = get(url, headers={"Authorization": f"Bearer {token}"},
                timeout=timeout, follow_redirects=True)
    except Exception as e:  # transport/DNS/TLS error
        return False, f"could not reach relay URL: {e}"
    if 200 <= r.status_code < 300:
        return True, "ok"
    if r.status_code in (401, 403):
        return False, (f"relay reachable but auth failed (HTTP {r.status_code}) — "
                       "the paired phone's token is stale; re-scan the QR")
    return False, f"relay returned HTTP {r.status_code}"
```

- [ ] **Step 4: Run → pass**

Run: `uv run pytest tests/core/relay/test_health.py -q 2>&1 | tail -5`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit + journal**

```bash
git add src/mship/core/relay/health.py tests/core/relay/test_health.py
git commit -m "feat(relay): pure verify_relay_reachable health probe"
mship journal "relay: reachability verifier (pure, tested)" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=4 -->
### Task 4: Wire `_serve_with_relay` — per-device URL, verified + loud

**Files:**
- Modify: `src/mship/cli/serve.py`

Integration wiring (compile/smoke + full suite green; the testable logic landed in Tasks 1–3).

- [ ] **Step 1: Use the per-device subdomain + capture log**

In `_serve_with_relay` (`serve.py`), replace the subdomain/URL block. Import the new helpers and `relay_public_key`, compute the per-device id, point the tunnel at a captured log:

```python
    from mship.core.relay.keys import ensure_relay_key, relay_public_key
    from mship.core.relay.tunnel import (
        TunnelSupervisor, build_tunnel_argv, device_id, device_subdomain,
    )
    from mship.core.relay.health import verify_relay_reachable
    # ... existing imports ...

    key_path = ensure_relay_key(home=Path.home())
    dev = device_id(relay_public_key(key_path))
    subdomain = device_subdomain(workspace, dev)          # was: subdomain_for(workspace)
    argv = build_tunnel_argv(rc, subdomain=subdomain, local_port=port, key_path=key_path)

    public_url = f"https://{subdomain}.{rc.host}"
    link = build_pair_link(url=public_url, token=token, workspace=workspace)

    log_path = workspace_root / ".mothership" / "relay-tunnel.log"
    log_path.unlink(missing_ok=True)                      # fresh per run
    sup = TunnelSupervisor(argv=argv, log_path=log_path)
    sup.start()
```

(Delete the old `subdomain = subdomain_for(workspace)` and `key_path = ensure_relay_key(...)` lines being replaced; keep the rest.)

- [ ] **Step 2: Verify after startup + warn on respawn (loud, not silent)**

Replace the tick loop + the optimistic prints. The tick loop now also warns when the tunnel keeps dying; add a one-shot background verifier that waits for the local server, then probes the public URL:

```python
    import time

    def _tick_loop():
        warned = False
        while not stop_event.wait(relay_tick):
            try:
                sup.tick()
                if sup.restart_count >= 3 and not warned:
                    warned = True
                    output.error(
                        "relay tunnel keeps dropping (restarted "
                        f"{sup.restart_count}×). Last ssh output:\n"
                        + sup.recent_output().strip()
                    )
            except Exception:
                pass

    def _verify_loop():
        # wait for uvicorn to answer locally, then probe the PUBLIC url end-to-end.
        local = f"http://{host}:{port}/health"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not stop_event.is_set():
            try:
                import httpx
                httpx.get(local, headers={"Authorization": f"Bearer {token}"}, timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        ok, detail = verify_relay_reachable(public_url, token)
        if ok:
            output.success(f"✓ relay reachable: {public_url}")
        else:
            tail = sup.recent_output().strip()
            output.error(f"✗ relay NOT reachable: {detail}"
                         + (f"\nssh tunnel output:\n{tail}" if tail else ""))

    ticker = threading.Thread(target=_tick_loop, name="mship-relay-tick", daemon=True)
    ticker.start()
    threading.Thread(target=_verify_loop, name="mship-relay-verify", daemon=True).start()

    output.print(f"mship serve → http://{host}:{port}  (auth: bearer token; docs: disabled)")
    output.print(f"relay → {public_url}  (per-device; tunnel via ssh -R to {rc.host})")
    output.print(link)
    typer.echo(segno.make(link).terminal(compact=True))
```

(Keep the existing `stop_event`, `uvicorn.run(...)`, and `finally: stop_event.set(); sup.stop()`. `output.success`/`output.error` already exist on `Output`; if `success` is absent, use `output.print` with a `✓`/`✗` prefix.)

- [ ] **Step 3: Build + full suite**

Run: `cd /home/bailey/development/repos/mship-workspace/.worktrees/relay-multi-device-identity-loud/mothership && uv run python -c "import mship.cli.serve" && mship test --repos mothership`
Expected: imports clean; full pytest suite green (Tasks 1–3 tests + existing).

- [ ] **Step 4: Commit + journal**

```bash
git add src/mship/cli/serve.py
git commit -m "feat(relay): serve uses per-device URL, verifies reachability, surfaces tunnel failures"
mship journal "relay: serve wiring — per-device URL + ✓/✗ health-check + loud respawn" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=5 -->
### Task 5: Verification + finish

- [ ] **Step 1: Full suite** — `mship test --repos mothership` → all green.
- [ ] **Step 2: Smoke (manual, optional)** — `mship serve --relay-host mship-relay.atomikpanda.com` in this workspace prints `relay → https://mship-workspace-<id>.…` and, shortly after, `✓ relay reachable: …`; re-scan the new QR in Ground Control to confirm it connects. Stop any other device's serve of the same workspace and confirm no collision (distinct subdomains).
- [ ] **Step 3:** `mship journal "relay hardening complete; suite green" --action verified --test-state pass` then `mship phase review`.

<!-- /mship:task -->

---

## Self-Review

**Coverage:** per-device identity → Task 1; surface-the-real-reason (capture ssh output) → Task 2; verified-not-optimistic (`✓/✗` probe) → Task 3 (pure) + Task 4 (wired); loud respawn → Task 4. The original incident (collision + silent token mismatch) is fixed by Task 1 (unique URL) + Task 4 (the `verify_relay_reachable` 401 path explicitly says "the paired phone's token is stale; re-scan the QR").

**Placeholder scan:** none — concrete code/commands throughout. Task 4 is integration wiring with full code shown; its logic is the already-tested Task 1–3 units.

**Type/name consistency:** `device_id`/`device_subdomain` (T1) used in T4; `TunnelSupervisor(log_path=…)` + `recent_output()`/`restart_count` (T2) used in T4; `verify_relay_reachable` (T3) used in T4. `relay_public_key(key_path)` matches `keys.py`. `build_pair_link`/`segno`/`uvicorn` unchanged from current `serve.py`.

**Note (documented):** the per-device subdomain changes this machine's relay URL, so existing phones must re-pair once (the new QR). That's the intended, one-time cost of making it collision-free.
