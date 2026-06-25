# Relay Enrollment with Owner Approval (v1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `relay-enroll-approval` (mship spec). v1 = the secure core with CLI approve/deny on the relay host; phone (Ground Control) approval is v2.

**Goal:** Let a device that can't touch the relay box *request* relay access (public endpoint), have the request sit **pending** (never auto-enrolled), and let the owner **approve/deny** it from the relay host. Pending requests **expire after 30 min**.

**Architecture:** Security-critical logic is pure and unit-tested. `core/relay/enroll.py` holds pubkey validation, fingerprinting, traversal-proof label sanitization, and a filesystem `RequestStore` (create → pending; approve writes into the allowlist; deny/expire resolve) with an injectable clock + atomic writes. `core/relay/enroll_app.py` is a thin FastAPI wrapper (`POST /enroll`, `GET /status/{id}`). `cli/relay.py` adds `enroll-server`, `requests`, `approve`, `deny`, `enroll`. No sish change — approve just drops a file into the existing `pubkeys/` allowlist (sish re-reads it per connection).

**Tech Stack:** Python 3, FastAPI + uvicorn (already deps), httpx (requester), pytest (+ FastAPI `TestClient`). Injectable clock for TTL; no network in tests.

---

## Decisions (from spec open questions)
- **Store layout:** `pending/<id>.json` → moved to `resolved/<id>.json` (with a `status`) on approve/deny/expire. Atomic temp-file+rename. `/status` reads both.
- **Config:** CLI flags with defaults mirroring `docker/relay/` — `--pubkeys-dir ./pubkeys`, `--store-dir ./pending-store`, `--port 47180`, `--ttl 1800`.
- **Deployment:** a documented `mship relay enroll-server` host process for v1.
- **Anti-abuse:** open requests (approval is the gate) + a global pending cap (`MAX_PENDING=50`) + the 30-min TTL sweeping stale entries.

## Files

| File | Change |
|---|---|
| `src/mship/core/relay/enroll.py` (create) | `validate_pubkey`, `fingerprint`, `sanitize_label`, `RequestStore`, exceptions. |
| `src/mship/core/relay/enroll_app.py` (create) | `build_enroll_app(store)` → FastAPI. |
| `src/mship/cli/relay.py` (modify) | `enroll-server`, `requests`, `approve`, `deny`, `enroll`. |
| `tests/core/relay/test_enroll.py`, `tests/core/relay/test_enroll_app.py`, `tests/cli/test_relay_cli.py` | unit + app + CLI tests. |

Work in `.worktrees/relay-enrollment-with-approval-v1/mothership`, branch `feat/relay-enrollment-with-approval-v1`. Tests: `uv run pytest` / `mship test --repos mothership`.

---

<!-- mship:task id=1 -->
### Task 1: Pure helpers — validate / fingerprint / sanitize

**Files:** Create `src/mship/core/relay/enroll.py` (helpers only); Test `tests/core/relay/test_enroll.py`.

- [ ] **Step 1: failing tests**

```python
# tests/core/relay/test_enroll.py
from mship.core.relay.enroll import validate_pubkey, fingerprint, sanitize_label

_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAAAAAA host"

def test_validate_accepts_ssh_key():
    assert validate_pubkey(_PUB)
    assert validate_pubkey("ssh-rsa AAAAB3NzaC1yc2EAAAAD host")

def test_validate_rejects_junk():
    assert not validate_pubkey("not a key")
    assert not validate_pubkey("")
    assert not validate_pubkey("ssh-ed25519 !!!notbase64!!!")
    assert not validate_pubkey("rm -rf /")

def test_fingerprint_is_stable_sha256():
    fp = fingerprint(_PUB)
    assert fp.startswith("SHA256:")
    assert fp == fingerprint(_PUB + "  different-comment")  # body only

def test_sanitize_label_is_traversal_proof():
    assert sanitize_label("../../etc/passwd") == "etc-passwd"
    assert sanitize_label("My Laptop!") == "my-laptop"
    assert sanitize_label("") == "device"
    s = sanitize_label("a/" * 100)
    assert "/" not in s and ".." not in s and len(s) <= 40
```

- [ ] **Step 2: run → fail** — `uv run pytest tests/core/relay/test_enroll.py -q` → FAIL (module missing).

- [ ] **Step 3: implement**

```python
# src/mship/core/relay/enroll.py
from __future__ import annotations
import base64
import hashlib
import re


def validate_pubkey(s: str) -> bool:
    """True if `s` is a single ssh public-key line (key-type + valid base64 body)."""
    parts = s.strip().split()
    if len(parts) < 2:
        return False
    ktype, body = parts[0], parts[1]
    if not ktype.startswith(("ssh-", "ecdsa-", "sk-")):
        return False
    try:
        base64.b64decode(body, validate=True)
    except Exception:
        return False
    return len(body) >= 20


def fingerprint(pubkey: str) -> str:
    """ssh-keygen-style SHA256 fingerprint of the key body: `SHA256:<base64-no-pad>`."""
    body = pubkey.strip().split()[1]
    digest = hashlib.sha256(base64.b64decode(body)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def sanitize_label(hostname: str) -> str:
    """A safe pubkeys-filename stem from a hostname: lowercase [a-z0-9-], no traversal, ≤40."""
    s = re.sub(r"[^a-z0-9]+", "-", (hostname or "").lower()).strip("-")[:40].strip("-")
    return s or "device"
```

- [ ] **Step 4: run → pass** — `uv run pytest tests/core/relay/test_enroll.py -q` → PASS (4).

- [ ] **Step 5: commit + journal**
```bash
git add src/mship/core/relay/enroll.py tests/core/relay/test_enroll.py
git commit -m "feat(relay): enroll helpers — validate_pubkey/fingerprint/sanitize_label"
mship journal "enroll: pure helpers; 4 tests" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2: RequestStore — pending → approve/deny/expire (the heart)

**Files:** Modify `src/mship/core/relay/enroll.py` (append the store); Test `tests/core/relay/test_enroll.py`.

- [ ] **Step 1: failing tests**

```python
# append to tests/core/relay/test_enroll.py
import pytest
from mship.core.relay.enroll import RequestStore, PendingCapReached, NotPending

class _Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t

def _store(tmp_path, ttl=1800, clock=None, cap=50):
    return RequestStore(tmp_path / "store", ttl_seconds=ttl, max_pending=cap, clock=clock or _Clock())

def test_create_then_pending_then_approve_writes_allowlist(tmp_path):
    pubkeys = tmp_path / "pubkeys"; pubkeys.mkdir()
    s = _store(tmp_path)
    rid = s.create(_PUB, "my-laptop")
    assert s.get(rid) == "pending"
    assert [r["id"] for r in s.list_pending()] == [rid]
    s.approve(rid, pubkeys)
    assert s.get(rid) == "approved"
    written = list(pubkeys.glob("*.pub"))
    assert len(written) == 1 and written[0].read_text().strip() == _PUB.strip()
    assert s.list_pending() == []  # no longer pending

def test_deny_resolves_without_touching_allowlist(tmp_path):
    pubkeys = tmp_path / "pubkeys"; pubkeys.mkdir()
    s = _store(tmp_path)
    rid = s.create(_PUB, "h")
    s.deny(rid)
    assert s.get(rid) == "denied"
    assert list(pubkeys.glob("*.pub")) == []

def test_expiry_after_ttl(tmp_path):
    clock = _Clock(1000.0)
    s = _store(tmp_path, ttl=1800, clock=clock)
    rid = s.create(_PUB, "h")
    clock.t = 1000.0 + 1801  # past TTL
    assert s.list_pending() == []
    assert s.get(rid) == "expired"
    with pytest.raises(NotPending):
        s.approve(rid, tmp_path)

def test_pending_cap_enforced(tmp_path):
    s = _store(tmp_path, cap=2)
    s.create(_PUB, "a"); s.create(_PUB, "b")
    with pytest.raises(PendingCapReached):
        s.create(_PUB, "c")

def test_same_hostname_does_not_clobber(tmp_path):
    pubkeys = tmp_path / "pubkeys"; pubkeys.mkdir()
    s = _store(tmp_path)
    r1 = s.create(_PUB, "laptop"); s.approve(r1, pubkeys)
    r2 = s.create(_PUB, "laptop"); s.approve(r2, pubkeys)
    assert len(list(pubkeys.glob("*.pub"))) == 2  # unique filenames
```

- [ ] **Step 2: run → fail**

- [ ] **Step 3: implement (append to `enroll.py`)**

```python
import json
import secrets
import time
from pathlib import Path
from typing import Callable


class PendingCapReached(Exception):
    """Too many simultaneously-pending requests."""


class NotPending(Exception):
    """No pending request with that id (unknown, already resolved, or expired)."""


class RequestStore:
    """Filesystem-backed enroll requests: pending/<id>.json, moved to resolved/ on
    approve/deny/expire. Atomic writes; lazy TTL expiry on every read/mutate."""

    def __init__(self, base_dir, ttl_seconds: int = 1800, max_pending: int = 50,
                 clock: Callable[[], float] = time.time) -> None:
        self._pending = Path(base_dir) / "pending"
        self._resolved = Path(base_dir) / "resolved"
        self._pending.mkdir(parents=True, exist_ok=True)
        self._resolved.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl_seconds
        self._max_pending = max_pending
        self._clock = clock

    def _write_atomic(self, path: Path, rec: dict) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec))
        tmp.replace(path)

    def _resolve(self, p: Path, rec: dict, status: str) -> None:
        rec["status"] = status
        self._write_atomic(self._resolved / p.name, rec)
        p.unlink(missing_ok=True)

    def _sweep(self) -> None:
        now = self._clock()
        for p in self._pending.glob("*.json"):
            rec = json.loads(p.read_text())
            if now - rec["created_at"] >= self._ttl:
                self._resolve(p, rec, "expired")

    def create(self, pubkey: str, hostname: str) -> str:
        self._sweep()
        if len(list(self._pending.glob("*.json"))) >= self._max_pending:
            raise PendingCapReached()
        rid = secrets.token_hex(4)
        self._write_atomic(self._pending / f"{rid}.json", {
            "id": rid, "pubkey": pubkey.strip(), "hostname": hostname,
            "fingerprint": fingerprint(pubkey), "created_at": self._clock(),
            "status": "pending",
        })
        return rid

    def list_pending(self) -> list[dict]:
        self._sweep()
        return [json.loads(p.read_text()) for p in sorted(self._pending.glob("*.json"))]

    def get(self, rid: str) -> str:
        self._sweep()
        if (self._pending / f"{rid}.json").exists():
            return "pending"
        r = self._resolved / f"{rid}.json"
        if r.exists():
            return json.loads(r.read_text())["status"]
        return "unknown"

    def approve(self, rid: str, pubkeys_dir) -> None:
        self._sweep()
        p = self._pending / f"{rid}.json"
        if not p.exists():
            raise NotPending(rid)
        rec = json.loads(p.read_text())
        dest = _unique_pub_path(Path(pubkeys_dir), sanitize_label(rec["hostname"]))
        dest.write_text(rec["pubkey"] + "\n")
        self._resolve(p, rec, "approved")

    def deny(self, rid: str) -> None:
        p = self._pending / f"{rid}.json"
        if not p.exists():
            raise NotPending(rid)
        self._resolve(p, json.loads(p.read_text()), "denied")


def _unique_pub_path(pubkeys_dir: Path, stem: str) -> Path:
    pubkeys_dir.mkdir(parents=True, exist_ok=True)
    cand = pubkeys_dir / f"{stem}.pub"
    i = 2
    while cand.exists():
        cand = pubkeys_dir / f"{stem}-{i}.pub"
        i += 1
    return cand
```

- [ ] **Step 4: run → pass** (5 new tests + Task 1's 4).
- [ ] **Step 5: commit + journal**
```bash
git add src/mship/core/relay/enroll.py tests/core/relay/test_enroll.py
git commit -m "feat(relay): RequestStore — pending/approve/deny/expire with TTL + cap"
mship journal "enroll: RequestStore lifecycle + TTL + cap; tests green" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3: FastAPI enroll app

**Files:** Create `src/mship/core/relay/enroll_app.py`; Test `tests/core/relay/test_enroll_app.py`.

- [ ] **Step 1: failing tests**

```python
# tests/core/relay/test_enroll_app.py
from fastapi.testclient import TestClient
from mship.core.relay.enroll import RequestStore
from mship.core.relay.enroll_app import build_enroll_app

_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAAAAAA host"

def _client(tmp_path, cap=50):
    store = RequestStore(tmp_path / "s", max_pending=cap)
    return TestClient(build_enroll_app(store)), store

def test_enroll_creates_pending_and_status(tmp_path):
    c, _ = _client(tmp_path)
    r = c.post("/enroll", json={"pubkey": _PUB, "hostname": "laptop"})
    assert r.status_code == 200
    rid = r.json()["id"]
    assert c.get(f"/status/{rid}").json()["status"] == "pending"

def test_enroll_rejects_bad_key(tmp_path):
    c, _ = _client(tmp_path)
    assert c.post("/enroll", json={"pubkey": "garbage", "hostname": "x"}).status_code == 400

def test_enroll_over_cap_429(tmp_path):
    c, _ = _client(tmp_path, cap=1)
    c.post("/enroll", json={"pubkey": _PUB, "hostname": "a"})
    assert c.post("/enroll", json={"pubkey": _PUB, "hostname": "b"}).status_code == 429

def test_status_unknown(tmp_path):
    c, _ = _client(tmp_path)
    assert c.get("/status/deadbeef").json()["status"] == "unknown"
```

- [ ] **Step 2: run → fail**

- [ ] **Step 3: implement**

```python
# src/mship/core/relay/enroll_app.py
from __future__ import annotations
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mship.core.relay.enroll import RequestStore, PendingCapReached, validate_pubkey


class _EnrollBody(BaseModel):
    pubkey: str
    hostname: str = ""


def build_enroll_app(store: RequestStore) -> FastAPI:
    app = FastAPI(title="mship relay enroll")

    @app.post("/enroll")
    def enroll(body: _EnrollBody):
        if not validate_pubkey(body.pubkey):
            raise HTTPException(status_code=400, detail="invalid ssh public key")
        try:
            rid = store.create(body.pubkey, body.hostname)
        except PendingCapReached:
            raise HTTPException(status_code=429, detail="too many pending requests; try later")
        return {"id": rid, "status": "pending"}

    @app.get("/status/{rid}")
    def status(rid: str):
        return {"id": rid, "status": store.get(rid)}

    return app
```

- [ ] **Step 4: run → pass** (4).
- [ ] **Step 5: commit + journal**
```bash
git add src/mship/core/relay/enroll_app.py tests/core/relay/test_enroll_app.py
git commit -m "feat(relay): FastAPI enroll app (POST /enroll, GET /status)"
mship journal "enroll: FastAPI app + TestClient tests" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=4 -->
### Task 4: CLI — enroll-server / requests / approve / deny / enroll

**Files:** Modify `src/mship/cli/relay.py`; Test `tests/cli/test_relay_cli.py` (host-side commands + requester poll).

Host-side commands operate a `RequestStore` directly (the owner is on the box); `enroll` (requester) POSTs + polls via httpx (injectable for tests). Read the existing `cli/relay.py` `register(parent, get_container)` shape first.

- [ ] **Step 1: tests for the host commands + requester (TDD where pure)**

```python
# append to tests/cli/test_relay_cli.py
from mship.core.relay.enroll import RequestStore

def test_relay_requests_approve_deny_roundtrip(tmp_path, monkeypatch):
    store_dir = tmp_path / "store"; pubkeys = tmp_path / "pubkeys"; pubkeys.mkdir()
    s = RequestStore(store_dir)
    rid = s.create("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAA host", "laptop")
    base = ["relay", "--store-dir", str(store_dir), "--pubkeys-dir", str(pubkeys)]
    r = runner.invoke(app, base + ["requests"])
    assert r.exit_code == 0 and rid in r.output and "laptop" in r.output
    r = runner.invoke(app, base + ["approve", rid])
    assert r.exit_code == 0
    assert len(list(pubkeys.glob("*.pub"))) == 1  # key enrolled
    assert RequestStore(store_dir).get(rid) == "approved"
```

(The exact flag wiring — whether `--store-dir`/`--pubkeys-dir` are options on the `relay` group or per-command — is the implementer's call; match the codebase's typer conventions. The behavioral assertions above are the contract: `requests` lists, `approve` enrolls the key + resolves, `deny` resolves without enrolling.)

- [ ] **Step 2: implement the commands in `cli/relay.py`**

Add (sketch — adapt to the existing typer style):

```python
    @relay_app.command("enroll-server")
    def enroll_server(
        pubkeys_dir: str = typer.Option("./pubkeys", "--pubkeys-dir"),
        store_dir: str = typer.Option("./pending-store", "--store-dir"),
        port: int = typer.Option(47180, "--port"),
        host: str = typer.Option("0.0.0.0", "--host"),
        ttl: int = typer.Option(1800, "--ttl", help="pending request TTL seconds (default 30m)"),
    ):
        """Run the public enroll endpoint on the relay host (devices POST their key here)."""
        import uvicorn
        from pathlib import Path
        from mship.core.relay.enroll import RequestStore
        from mship.core.relay.enroll_app import build_enroll_app
        store = RequestStore(Path(store_dir), ttl_seconds=ttl)
        # approve happens out-of-band via `mship relay approve`; the server only creates pending.
        Output().print(f"enroll-server → http://{host}:{port}  (pubkeys: {pubkeys_dir}, ttl: {ttl}s)")
        uvicorn.run(build_enroll_app(store), host=host, port=port)

    @relay_app.command("requests")
    def requests_cmd(store_dir: str = typer.Option("./pending-store", "--store-dir")):
        """List pending enroll requests (id · hostname · fingerprint · age)."""
        from pathlib import Path
        from mship.core.relay.enroll import RequestStore
        out = Output()
        for r in RequestStore(Path(store_dir)).list_pending():
            out.print(f"{r['id']}  {r['hostname'] or '-'}  {r['fingerprint']}")

    @relay_app.command("approve")
    def approve_cmd(rid: str = typer.Argument(...),
                    store_dir: str = typer.Option("./pending-store", "--store-dir"),
                    pubkeys_dir: str = typer.Option("./pubkeys", "--pubkeys-dir")):
        """Approve a pending request: add its key to the allowlist (sish picks it up, no restart)."""
        from pathlib import Path
        from mship.core.relay.enroll import RequestStore, NotPending
        try:
            RequestStore(Path(store_dir)).approve(rid, Path(pubkeys_dir))
            Output().success(f"approved {rid} — enrolled into {pubkeys_dir}")
        except NotPending:
            Output().error(f"no pending request {rid!r} (unknown, already resolved, or expired)")
            raise typer.Exit(1)

    @relay_app.command("deny")
    def deny_cmd(rid: str = typer.Argument(...),
                 store_dir: str = typer.Option("./pending-store", "--store-dir")):
        """Deny a pending request (does not touch the allowlist)."""
        from pathlib import Path
        from mship.core.relay.enroll import RequestStore, NotPending
        try:
            RequestStore(Path(store_dir)).deny(rid)
            Output().print(f"denied {rid}")
        except NotPending:
            Output().error(f"no pending request {rid!r}"); raise typer.Exit(1)

    @relay_app.command("enroll")
    def enroll_cmd(
        enroll_url: str = typer.Option(..., "--enroll-url", help="http://<relay-host>:47180"),
        wait: bool = typer.Option(True, "--wait/--no-wait"),
    ):
        """From a NEW device: request relay access; the relay owner approves/denies."""
        import socket, time
        from pathlib import Path
        import httpx
        from mship.core.relay.keys import ensure_relay_key, relay_public_key
        out = Output()
        pub = relay_public_key(ensure_relay_key(home=Path.home())).strip()
        r = httpx.post(enroll_url.rstrip("/") + "/enroll",
                       json={"pubkey": pub, "hostname": socket.gethostname()}, timeout=10)
        if r.status_code != 200:
            out.error(f"enroll request failed: HTTP {r.status_code} {r.text}"); raise typer.Exit(1)
        rid = r.json()["id"]
        out.print(f"requested (id {rid}) — ask the relay owner to `mship relay approve {rid}`.")
        if not wait:
            return
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            st = httpx.get(enroll_url.rstrip("/") + f"/status/{rid}", timeout=10).json()["status"]
            if st == "approved":
                out.success("✓ approved — you can now run `mship serve --relay`."); return
            if st in ("denied", "expired"):
                out.error(f"✗ {st}."); raise typer.Exit(1)
            time.sleep(3)
        out.error("✗ timed out waiting for approval."); raise typer.Exit(1)
```

> Note the `--store-dir`/`--pubkeys-dir` test flags: if typer makes per-command options awkward to invoke as in the test, expose them as options on the `relay` sub-app callback (shared) and adjust the test invocation to match. The contract (the behavioral assertions) is what matters.

- [ ] **Step 3: build + suite** — `uv run python -c "import mship.cli.relay"` then `mship test --repos mothership` (green; the requester `enroll` poll is integration — keep its logic thin; the host commands are covered by the roundtrip test).

- [ ] **Step 4: commit + journal**
```bash
git add src/mship/cli/relay.py tests/cli/test_relay_cli.py
git commit -m "feat(relay): enroll-server + requests/approve/deny + requester enroll"
mship journal "enroll: CLI commands wired; roundtrip test green" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=5 -->
### Task 5: Verification + docs + finish

- [ ] **Step 1: full suite** — `mship test --repos mothership` → all green (Tasks 1–4 + existing).
- [ ] **Step 2: docs** — add a short "Enrolling a device that can't reach the relay box" section to `docs/relay-hosting.md`: owner runs `mship relay enroll-server` on the relay host; new device runs `mship relay enroll --enroll-url http://<relay-host>:47180`; owner `mship relay requests` + `approve <id>`. Note the 30-min TTL + that requests only become keys on approval.
- [ ] **Step 3:** `mship journal "relay enroll-approval v1 complete; suite green" --action verified --test-state pass` then `mship phase review`.
<!-- /mship:task -->

---

## Self-Review

**Coverage:** AC1/AC2 → Task 3 (enroll endpoint + bad-key 400 + cap 429) over Task 2's store. AC3 (30-min TTL) → Task 2 (`test_expiry_after_ttl`). AC4 (`requests`) / AC5 (`approve` enrolls + resolves, refuses expired) / AC6 (`deny`) → Tasks 2+4. AC7 (`enroll` requester, no box access) → Task 4. AC8 (sanitize, no traversal/clobber) → Task 1 (`sanitize_label`) + Task 2 (`_unique_pub_path`, `test_same_hostname_does_not_clobber`). AC9 (pure cores + app + requester tested) → Tasks 1–4 tests.

**Security check:** `POST /enroll` only ever creates a pending record (never writes pubkeys/); the allowlist is touched only by `approve` (owner, on the box). Bad keys 400; cap 429; TTL sweeps. Filenames go through `sanitize_label` + `_unique_pub_path` (traversal-proof, non-clobbering). Plain HTTP carries only a non-secret pubkey.

**Placeholder scan:** none — concrete code/commands; Task 4 flags the one typer-style adaptation (per-command vs group option) explicitly with the behavioral contract pinned by tests.

**Type consistency:** `validate_pubkey`/`fingerprint`/`sanitize_label` (T1) used by `RequestStore` (T2) + `build_enroll_app` (T3); `RequestStore`/`PendingCapReached`/`NotPending` (T2) used by T3 + T4; `build_enroll_app` (T3) used by `enroll-server` (T4). `ensure_relay_key`/`relay_public_key` match `keys.py`.
