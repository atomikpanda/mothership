# Low-Friction Phone Connection (self-hosted sish relay + QR pairing) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `low-friction-phone-connection-to-mship` (approved) — `specs/2026-06-15-low-friction-phone-connection-to-mship.md` in the workspace.

**Goal:** Connect the Ground Control phone app to any `mship serve` workspace by scanning a QR code, reachable from anywhere with no VPN — via a reverse SSH tunnel to a relay the user self-hosts (sish).

**Architecture:** `mship serve` stays bound to loopback; mship opens + supervises an `ssh -R <subdomain>:80:localhost:47100` reverse tunnel to the user's sish relay, which publishes a stable `https://<workspace>.<relay-domain>` with auto-TLS. Two auth layers: SSH-key allowlist on the relay (who may expose) + bearer token on the API (who may call). Tunnel/token/key/pairing live in reusable core modules so a future auto-serve daemon can drive them with no re-pairing.

**Tech Stack:** Python (mothership CLI/core), `segno` (pure-Python QR), system `ssh`/`ssh-keygen` via subprocess, sish (Docker) for the relay; Kotlin/Compose + CameraX/ML Kit (ground-control app QR scan).

**Phasing:** A (relay kit) → B (mship client tunnel + token/key + CLI) → C (pairing QR in app). Each phase is independently testable; A+B give end-to-end reachability (manual URL/token), C removes the typing.

**Repos:** `mothership` (Phases A, B, and the CLI side of C) and `ground-control` (the app side of C). Spawn a task scoped to both: `mship spawn "low-friction phone connection (sish relay + QR)" --repos mothership,ground-control`. Android tasks need `source ~/toolchains/android-env.sh`.

---

## File Structure

**mothership** (new `relay` core package keeps the daemon-reusable seam — ac8):
```
docker/relay/docker-compose.yml          # sish relay (auto-TLS)
scripts/relay-bootstrap.sh               # one-shot VPS bring-up
docs/relay-hosting.md                    # DNS wildcard + key allowlist + run
src/mship/core/relay/__init__.py
src/mship/core/relay/config.py           # RelayConfig (mothership.yaml `relay:` block)
src/mship/core/relay/token.py            # ensure_serve_token() persist/load
src/mship/core/relay/keys.py             # ensure_relay_key() / relay_public_key()
src/mship/core/relay/tunnel.py           # subdomain_for(), build_tunnel_argv(), TunnelSupervisor
src/mship/core/relay/pairing.py          # build_pair_link(), parse_pair_link()
src/mship/cli/relay.py                   # `mship relay setup`
src/mship/cli/pair.py                    # `mship pair` (QR)
# modified: src/mship/cli/serve.py       # `--relay` flag wires the above
# modified: src/mship/core/config.py     # parse `relay:` block
tests/core/relay/test_*.py
```

**ground-control:**
```
app/src/main/java/com/atomikpanda/groundcontrol/data/PairLink.kt   # parse groundcontrol://add
app/src/main/java/com/atomikpanda/groundcontrol/ui/settings/ScanConnectionScreen.kt
# modified: AndroidManifest.xml (intent-filter), SettingsScreen.kt (Scan button)
app/src/test/java/com/atomikpanda/groundcontrol/PairLinkTest.kt
```

Every commit step pairs with `mship journal "<what>" --action committed` from the worktree.

---

# PHASE A — Relay hosting kit (mothership)

## Task A1: Self-hostable sish relay (compose + bootstrap + docs)

**Files:**
- Create: `docker/relay/docker-compose.yml`, `scripts/relay-bootstrap.sh`, `docs/relay-hosting.md`

No unit tests (ops artifacts); verified by config validation + shellcheck.

- [ ] **Step 1: Create `docker/relay/docker-compose.yml`**
```yaml
# Self-hosted sish relay for mship serve reverse tunnels.
# Requires: a wildcard DNS record *.RELAY_DOMAIN -> this host, ports 80/443/2222 open.
services:
  sish:
    image: antoniomika/sish:latest
    restart: unless-stopped
    ports:
      - "2222:2222"   # SSH (clients open reverse tunnels here)
      - "80:80"       # HTTP (ACME challenges + redirect)
      - "443:443"     # HTTPS (public app traffic)
    volumes:
      - ./pubkeys:/pubkeys:ro       # allow-listed client public keys
      - ./keys:/keys                # sish host keys (persisted)
      - ./acme:/acme                # Let's Encrypt cert cache
    command:
      - --ssh-address=:2222
      - --http-address=:80
      - --https-address=:443
      - --https=true
      - --https-ondemand-certificate=true
      - --https-ondemand-certificate-email=${ACME_EMAIL}
      - --domain=${RELAY_DOMAIN}
      - --authentication=true
      - --authentication-keys-directory=/pubkeys
      - --bind-random-subdomains=false      # honor the requested subdomain
      - --bind-random-ports=false
      - --idle-connection=false
```

- [ ] **Step 2: Create `scripts/relay-bootstrap.sh`**
```bash
#!/usr/bin/env bash
# One-shot bring-up of a self-hosted sish relay on a fresh Debian/Ubuntu VPS.
# Usage: RELAY_DOMAIN=relay.example.com ACME_EMAIL=you@example.com ./relay-bootstrap.sh
set -euo pipefail
: "${RELAY_DOMAIN:?set RELAY_DOMAIN (the wildcard base, e.g. relay.example.com)}"
: "${ACME_EMAIL:?set ACME_EMAIL (for Let's Encrypt)}"

if ! command -v docker >/dev/null 2>&1; then
  echo "[bootstrap] installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
HERE="$(cd "$(dirname "$0")/../docker/relay" && pwd)"
mkdir -p "$HERE/pubkeys" "$HERE/keys" "$HERE/acme"
echo "[bootstrap] add client public keys to $HERE/pubkeys/ (one file per key), then:"
echo "  RELAY_DOMAIN=$RELAY_DOMAIN ACME_EMAIL=$ACME_EMAIL docker compose -f $HERE/docker-compose.yml up -d"
echo "[bootstrap] DNS: point  *.$RELAY_DOMAIN  A record at this host's public IP."
RELAY_DOMAIN="$RELAY_DOMAIN" ACME_EMAIL="$ACME_EMAIL" docker compose -f "$HERE/docker-compose.yml" up -d
echo "[bootstrap] sish is up. Test a tunnel: ssh -p 2222 -R myws:80:localhost:8000 $RELAY_DOMAIN"
```

- [ ] **Step 3: Create `docs/relay-hosting.md`** — a concise runbook: prerequisites (a VPS, a domain), the wildcard DNS record (`*.relay.example.com A <ip>`), open ports (80/443/2222), running the bootstrap, adding client pubkeys (`mship relay setup` prints the line), and a verification `ssh -p 2222 -R test:80:localhost:8000 relay.example.com`. (Full prose — no placeholders; mirror Steps 1–2 exactly.)

- [ ] **Step 4: Validate + shellcheck**

Run:
```bash
cd /path/to/mothership/worktree
docker compose -f docker/relay/docker-compose.yml config >/dev/null && echo "compose OK"   # if docker present
shellcheck scripts/relay-bootstrap.sh && echo "shellcheck OK"   # if shellcheck present; else: bash -n
chmod +x scripts/relay-bootstrap.sh
```
Expected: `compose OK` (or skip if no docker), `shellcheck OK` (or `bash -n` clean).

- [ ] **Step 5: Commit**
```bash
git add docker/ scripts/relay-bootstrap.sh docs/relay-hosting.md
git commit -m "feat(relay): self-hostable sish relay kit (compose + bootstrap + docs)"
mship journal "relay hosting kit (sish compose + bootstrap + docs)" --action committed
```

---

# PHASE B — mship client integration (mothership)

## Task B1: Relay config (`relay:` block in mothership.yaml)

**Files:**
- Create: `src/mship/core/relay/__init__.py` (empty), `src/mship/core/relay/config.py`
- Modify: `src/mship/core/config.py` (parse `relay:` into the workspace config)
- Test: `tests/core/relay/test_config.py`

- [ ] **Step 1: Write the failing test**
```python
# tests/core/relay/test_config.py
from mship.core.relay.config import RelayConfig

def test_from_mapping_full():
    rc = RelayConfig.from_mapping({"host": "relay.example.com", "ssh_port": 2222, "user": "tunnel"})
    assert rc.host == "relay.example.com"
    assert rc.ssh_port == 2222
    assert rc.user == "tunnel"

def test_from_mapping_defaults_and_none():
    assert RelayConfig.from_mapping(None) is None           # no relay configured
    rc = RelayConfig.from_mapping({"host": "r.example.com"})
    assert rc.ssh_port == 2222 and rc.user is None          # defaults
```

- [ ] **Step 2: Run, verify fail** — `uv run pytest tests/core/relay/test_config.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `src/mship/core/relay/config.py`**
```python
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class RelayConfig:
    host: str
    ssh_port: int = 2222
    user: str | None = None       # ssh user; None → ssh default

    @staticmethod
    def from_mapping(data: dict | None) -> "RelayConfig | None":
        if not data:
            return None
        host = data.get("host")
        if not host:
            raise ValueError("relay.host is required when a `relay:` block is present")
        return RelayConfig(
            host=host,
            ssh_port=int(data.get("ssh_port", 2222)),
            user=data.get("user"),
        )
```

- [ ] **Step 4: Run, verify pass.** Then wire into `config.py`: where `WorkspaceConfig` is built, add `relay = RelayConfig.from_mapping(raw.get("relay"))` and expose it as an optional attribute `relay: RelayConfig | None = None`. Add a test that `ConfigLoader.load` on a yaml with a `relay:` block exposes `config.relay.host`. (Match the existing config-parsing pattern in `src/mship/core/config.py`.)

- [ ] **Step 5: Commit** — `feat(relay): relay config block` + journal.

## Task B2: Per-workspace serve token (auto-generate + persist)

**Files:** Create `src/mship/core/relay/token.py`; Test `tests/core/relay/test_token.py`

- [ ] **Step 1: Failing test**
```python
from pathlib import Path
from mship.core.relay.token import ensure_serve_token

def test_generates_and_persists(tmp_path: Path):
    t1 = ensure_serve_token(tmp_path)            # tmp_path = workspace root
    assert isinstance(t1, str) and len(t1) >= 32
    assert (tmp_path / ".mothership" / "serve-token").read_text().strip() == t1
    t2 = ensure_serve_token(tmp_path)            # stable across calls
    assert t2 == t1

def test_respects_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "explicit")
    assert ensure_serve_token(tmp_path) == "explicit"   # env wins, no file write
    assert not (tmp_path / ".mothership" / "serve-token").exists()
```

- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement `token.py`**
```python
from __future__ import annotations
import os, secrets
from pathlib import Path

def ensure_serve_token(workspace_root: Path) -> str:
    """Return the serve bearer token: env override > persisted file > freshly generated+persisted."""
    env = os.environ.get("MSHIP_SERVE_TOKEN")
    if env:
        return env
    path = workspace_root / ".mothership" / "serve-token"
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token
```

- [ ] **Step 4: Verify pass.** **Step 5: Commit** `feat(relay): per-workspace serve token` + journal.

## Task B3: Relay SSH key management

**Files:** Create `src/mship/core/relay/keys.py`; Test `tests/core/relay/test_keys.py`

Generate a dedicated ed25519 key via `ssh-keygen` if absent; expose the public key. Inject the runner for testability.

- [ ] **Step 1: Failing test**
```python
from pathlib import Path
from mship.core.relay.keys import ensure_relay_key, relay_public_key

def test_generates_key_when_absent(tmp_path):
    calls = []
    def fake_run(argv):                      # stand in for subprocess
        calls.append(argv)
        key = tmp_path / "relay_ed25519"
        key.write_text("PRIV"); (key.with_suffix(".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
        return 0
    path = ensure_relay_key(home=tmp_path, runner=fake_run)
    assert path == tmp_path / ".mothership" / "relay_ed25519" or path.name == "relay_ed25519"
    assert any("ssh-keygen" in a for a in calls[0])
    assert relay_public_key(path).startswith("ssh-ed25519 ")

def test_idempotent_when_present(tmp_path):
    # pre-create the key; runner must NOT be called
    ...
```

- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement `keys.py`** — `ensure_relay_key(home, runner=subprocess-based default)` computes `home/.mothership/relay_ed25519`, returns it if present, else `runner(["ssh-keygen","-t","ed25519","-f",str(path),"-N","","-C","mship-relay"])` and returns it; `relay_public_key(path)` reads `path.with_suffix(".pub")` (or `<path>.pub`). Default runner wraps `subprocess.run(..., check=True).returncode`.
- [ ] **Step 4: Verify pass.** **Step 5: Commit** `feat(relay): dedicated relay ssh key mgmt` + journal.

## Task B4: Subdomain + tunnel argv (pure)

**Files:** Create `src/mship/core/relay/tunnel.py` (subdomain + argv only here; supervisor in B5); Test `tests/core/relay/test_tunnel_args.py`

- [ ] **Step 1: Failing test**
```python
from pathlib import Path
from mship.core.relay.config import RelayConfig
from mship.core.relay.tunnel import subdomain_for, build_tunnel_argv

def test_subdomain_slugs_workspace():
    assert subdomain_for("Mship Workspace") == "mship-workspace"

def test_build_tunnel_argv():
    rc = RelayConfig(host="relay.example.com", ssh_port=2222, user="tunnel")
    argv = build_tunnel_argv(rc, subdomain="mship-workspace", local_port=47100, key_path=Path("/k/relay_ed25519"))
    assert argv[0] == "ssh"
    assert "-R" in argv and "mship-workspace:80:localhost:47100" in argv
    assert "-p" in argv and "2222" in argv
    assert "-i" in argv and "/k/relay_ed25519" in argv
    assert argv[-1] == "tunnel@relay.example.com"
    # resilience options present
    assert "-o" in argv and "ExitOnForwardFailure=yes" in argv and "ServerAliveInterval=30" in argv
```

- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement** in `tunnel.py`:
```python
from __future__ import annotations
from pathlib import Path
from mship.core.relay.config import RelayConfig
from mship.util.slug import slugify

def subdomain_for(workspace: str) -> str:
    return slugify(workspace)

def build_tunnel_argv(rc: RelayConfig, *, subdomain: str, local_port: int, key_path: Path) -> list[str]:
    target = f"{rc.user}@{rc.host}" if rc.user else rc.host
    return [
        "ssh",
        "-p", str(rc.ssh_port),
        "-i", str(key_path),
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-N",
        "-R", f"{subdomain}:80:localhost:{local_port}",
        target,
    ]
```

- [ ] **Step 4: Verify pass.** **Step 5: Commit** `feat(relay): subdomain + ssh tunnel argv builder` + journal.

## Task B5: TunnelSupervisor (start / supervise / stop)

**Files:** Add `TunnelSupervisor` to `src/mship/core/relay/tunnel.py`; Test `tests/core/relay/test_tunnel_supervisor.py`

Supervises the ssh subprocess: start, restart on unexpected exit (capped backoff), stop cleanly. Inject a process factory for testing (no real ssh).

- [ ] **Step 1: Failing test**
```python
from mship.core.relay.tunnel import TunnelSupervisor

class FakeProc:
    def __init__(self): self._alive = True; self.terminated = False
    def poll(self): return None if self._alive else 0
    def terminate(self): self.terminated = True; self._alive = False
    def wait(self, timeout=None): self._alive = False; return 0

def test_start_then_stop_terminates_process():
    procs = []
    def factory(argv): p = FakeProc(); procs.append(p); return p
    sup = TunnelSupervisor(argv=["ssh", "..."], proc_factory=factory)
    sup.start()
    assert sup.is_running() and len(procs) == 1
    sup.stop()
    assert procs[0].terminated and not sup.is_running()
```
(A second test: `restart_on_unexpected_exit` — flip the proc's poll() to a nonzero exit and assert the supervisor spawns a replacement when `tick()` is called. Keep restart logic tick-driven so it's unit-testable without threads.)

- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement** a tick-driven `TunnelSupervisor` (start spawns via `proc_factory`; `tick()` checks `poll()` and respawns with capped backoff; `stop()` terminates + waits; `is_running()` reflects state). The CLI run loop calls `tick()` on an interval; the default `proc_factory` uses `subprocess.Popen` with process-group setup mirroring `src/mship/core/executor.py`'s background-service handling. Keep all policy in pure methods.
- [ ] **Step 4: Verify pass.** **Step 5: Commit** `feat(relay): tunnel supervisor (reconnect/teardown)` + journal.

## Task B6: Pairing link (encode/decode, shared core)

**Files:** Create `src/mship/core/relay/pairing.py`; Test `tests/core/relay/test_pairing.py`

- [ ] **Step 1: Failing test**
```python
from mship.core.relay.pairing import build_pair_link, parse_pair_link

def test_round_trip():
    link = build_pair_link(url="https://ws.relay.example.com", token="tok en/+=", workspace="ws")
    assert link.startswith("groundcontrol://add?")
    p = parse_pair_link(link)
    assert p == {"url": "https://ws.relay.example.com", "token": "tok en/+=", "workspace": "ws"}

def test_parse_rejects_wrong_scheme():
    import pytest
    with pytest.raises(ValueError):
        parse_pair_link("https://add?url=x")
```

- [ ] **Step 2: Verify fail.**
- [ ] **Step 3: Implement `pairing.py`** using `urllib.parse` (`urlencode` with `quote_via=quote`, and `urlparse`/`parse_qs`); `parse_pair_link` validates scheme == `groundcontrol` and netloc/path == `add`, returns `{url, token, workspace}`, raising `ValueError` on mismatch or missing keys.
- [ ] **Step 4: Verify pass.** **Step 5: Commit** `feat(relay): pairing deep-link encode/decode` + journal.

## Task B7: Wire `mship serve --relay`, `mship relay setup`, `mship pair`

**Files:** Modify `src/mship/cli/serve.py`; Create `src/mship/cli/relay.py`, `src/mship/cli/pair.py`; add `segno` to `pyproject.toml`; Test `tests/cli/test_relay_cli.py`

- [ ] **Step 1: Add `segno` dep** — `uv add segno`; verify `uv run python -c "import segno"`.
- [ ] **Step 2: Failing test (pairing QR command)**
```python
# tests/cli/test_relay_cli.py — `mship pair` prints a scannable link + QR for a configured relay
from typer.testing import CliRunner
from mship.cli import app
runner = CliRunner()
def test_pair_outputs_deeplink(relay_configured_workspace):   # fixture: workspace + relay.host + a serve token
    r = runner.invoke(app, ["pair"])
    assert r.exit_code == 0
    assert "groundcontrol://add?" in r.output
    assert "workspace=" in r.output
```

- [ ] **Step 3: Implement**
  - `mship pair` (`src/mship/cli/pair.py`): compute `url = https://{subdomain_for(workspace)}.{rc.host}` (the sish `--domain` is the same hostname clients SSH to), `token = ensure_serve_token(workspace_root)`, `link = build_pair_link(...)`; print the link and render a QR with `segno.make(link).terminal(compact=True)`.
  - `mship relay setup` (`src/mship/cli/relay.py`): `ensure_relay_key(home=Path.home())`, print `relay_public_key(path)` with the instruction to drop it in the relay's `pubkeys/` dir.
  - `mship serve --relay [<host>]` (modify `serve.py`): when `--relay`/`config.relay` is set — require/auto-generate the token (`ensure_serve_token`), bind serve to loopback as today, build argv via `ensure_relay_key` + `build_tunnel_argv`, start a `TunnelSupervisor`, print the public URL + pairing QR, then run uvicorn; `tick()` the supervisor (background thread or signal-driven) and on shutdown call `sup.stop()`. Reuse the existing non-loopback/token guard.
- [ ] **Step 4: Verify** — `uv run pytest tests/cli/test_relay_cli.py -q` green; manual: `mship serve --relay` against a real relay prints a reachable URL.
- [ ] **Step 5: Commit** `feat(serve): --relay tunnel + mship pair/relay setup (QR)` + journal.

---

# PHASE C — Pairing in the Ground Control app (ground-control)

> Android tasks: `source ~/toolchains/android-env.sh` before gradle.

## Task C1: Deep-link parser (Kotlin, pure)

**Files:** Create `app/src/main/java/com/atomikpanda/groundcontrol/data/PairLink.kt`; Test `app/src/test/java/com/atomikpanda/groundcontrol/PairLinkTest.kt`

- [ ] **Step 1: Failing test**
```kotlin
package com.atomikpanda.groundcontrol
import com.atomikpanda.groundcontrol.data.PairLink
import org.junit.Assert.*
import org.junit.Test

class PairLinkTest {
    @Test fun parses_valid_link() {
        val c = PairLink.parse("groundcontrol://add?url=https%3A%2F%2Fws.relay.example.com&token=abc&workspace=ws")
        assertNotNull(c)
        assertEquals("https://ws.relay.example.com", c!!.baseUrl)
        assertEquals("abc", c.token)
        assertEquals("ws", c.workspaceName)
    }
    @Test fun rejects_wrong_scheme_or_missing_fields() {
        assertNull(PairLink.parse("https://add?url=x"))
        assertNull(PairLink.parse("groundcontrol://add?token=abc"))   // no url
    }
}
```

- [ ] **Step 2: Verify fail** — `./gradlew testDebugUnitTest --tests "*PairLinkTest"` → RED.
- [ ] **Step 3: Implement `PairLink.kt`**
```kotlin
package com.atomikpanda.groundcontrol.data
import android.net.Uri
import java.util.UUID

object PairLink {
    /** Parse a groundcontrol://add?url=&token=&workspace= deep link into a connection, or null if invalid. */
    fun parse(raw: String): WorkspaceConnection? {
        val uri = runCatching { Uri.parse(raw) }.getOrNull() ?: return null
        if (uri.scheme != "groundcontrol" || uri.host != "add") return null
        val url = uri.getQueryParameter("url")?.takeIf { it.isNotBlank() } ?: return null
        val token = uri.getQueryParameter("token")?.takeIf { it.isNotBlank() }
        val ws = uri.getQueryParameter("workspace").orEmpty()
        return WorkspaceConnection(id = UUID.randomUUID().toString(), baseUrl = url, token = token, workspaceName = ws)
    }
}
```
Note: `Uri` is available in JVM unit tests via Robolectric OR is `android.net.Uri` (not in plain JVM). If the existing test setup lacks Robolectric, parse with `java.net.URI` + manual query split instead (keep the same signature + tests). Decide based on the repo's test deps; prefer `java.net.URI` to stay pure-JVM (no Robolectric).

- [ ] **Step 4: Verify pass.** **Step 5: Commit** `feat(android): pairing deep-link parser` + journal.

## Task C2: Settings → Add → Scan QR + deep-link intent

**Files:** Create `ui/settings/ScanConnectionScreen.kt`; Modify `AndroidManifest.xml` (intent-filter for `groundcontrol://add`), `ui/settings/SettingsScreen.kt` (Scan button), `SettingsViewModel.kt` (add-from-link). Build-verified (no unit tests for camera UI).

- [ ] **Step 1: Add scanner dep** — ML Kit barcode scanning (`com.google.mlkit:barcode-scanning`) + CameraX (`androidx.camera:*`), or ZXing Android Embedded (`com.journeyapps:zxing-android-embedded`) for a simpler drop-in scanner activity. Prefer ZXing-embedded for least code.
- [ ] **Step 2: SettingsViewModel** — add `fun addFromLink(raw: String): Boolean { val c = PairLink.parse(raw) ?: return false; viewModelScope.launch { repo.upsert(c) }; return true }`.
- [ ] **Step 3: SettingsScreen** — add a "Scan QR" button that launches the scanner; on result, call `vm.addFromLink(result)` and toast success/failure.
- [ ] **Step 4: AndroidManifest.xml** — add an `<intent-filter>` on MainActivity for `<data android:scheme="groundcontrol" android:host="add"/>` (VIEW/BROWSABLE) so a tapped/pasted deep link opens the app; route it to `addFromLink`.
- [ ] **Step 5: Build** — `./gradlew assembleDebug` BUILD SUCCESSFUL; `./gradlew testDebugUnitTest` still green (PairLinkTest + the 15 prior).
- [ ] **Step 6: Commit** `feat(android): scan QR / deep-link to add a connection` + journal.

---

## Acceptance Criteria → Tasks

- ac1 (relay kit) → A1
- ac2 (`serve --relay` supervised tunnel, reconnect/teardown) → B5 + B7
- ac3 (relay host config + stable subdomain URL) → B1 + B4
- ac4 (dedicated SSH key + surface pubkey) → B3 + `mship relay setup` (B7)
- ac5 (token required when relaying; auto-gen/persist) → B2 + B7
- ac6 (`serve --relay`/`pair` prints QR deep link) → B6 + B7
- ac7 (app Scan QR / deep-link add, no typing) → C1 + C2
- ac8 (tunnel/token/key/config/pairing as reusable core, durable pairing) → the `src/mship/core/relay/` package (B1–B6), callable independent of `serve`
- ac9 (unit tests: payload codec both sides, token, subdomain, argv, config) → B1,B2,B3,B4,B6 tests + C1 test
