# Non-relay QR pairing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** `non-relay-qr-pairing` (approved) — `specs/2026-06-22-non-relay-qr-pairing.md`.

**Goal:** Non-relay `mship serve` (tailnet/LAN, with a token) prints a scannable pairing QR — the same `groundcontrol://add?...` link the relay emits — so users pair Ground Control by scanning instead of hand-typing URL + token. The app already scans this format; the only app change is a guard test.

**Architecture:** mothership-only functional change at the non-relay serve path, with the decision logic in a pure, testable `serve_pair` module (uvicorn never runs in tests). Reuses `build_pair_link` + segno (already used by the relay path). ground-control gets one `PairLinkTest` case proving an `http://` link parses (no functional change).

**This task spans TWO repos.** Each task says which worktree to work in:
- mothership: `.worktrees/non-relay-qr-pairing/mothership` (Tasks 1, 2)
- ground-control: `.worktrees/non-relay-qr-pairing/ground-control` (Task 3)
- Task 4 verifies both.

**dev-binary note:** run mothership tests via `uv run pytest …` so they exercise the worktree source.

---

<!-- mship:task id=1 -->
### Task 1 (mothership): `serve_pair` pure helpers

**Files:**
- Create: `src/mship/core/serve_pair.py`
- Test: `tests/core/test_serve_pair.py`

Work in the **mothership** worktree.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_serve_pair.py`:

```python
from mship.core.relay.pairing import parse_pair_link
from mship.core.serve_pair import resolve_advertised_host, serve_pair_link


def test_resolve_concrete_host_passthrough():
    assert resolve_advertised_host("192.168.1.50") == "192.168.1.50"
    assert resolve_advertised_host("host.tailnet.ts.net") == "host.tailnet.ts.net"


def test_resolve_loopback_is_none():
    for h in ("127.0.0.1", "localhost", "::1"):
        assert resolve_advertised_host(h) is None


def test_resolve_unspecified_uses_primary_ip():
    assert resolve_advertised_host("0.0.0.0", primary_ip=lambda: "100.1.2.3") == "100.1.2.3"
    assert resolve_advertised_host("::", primary_ip=lambda: "100.1.2.3") == "100.1.2.3"
    assert resolve_advertised_host("0.0.0.0", primary_ip=lambda: None) is None


def test_serve_pair_link_none_without_token():
    assert serve_pair_link("192.168.1.50", 47100, None, "ws") is None
    assert serve_pair_link("192.168.1.50", 47100, "", "ws") is None


def test_serve_pair_link_none_on_loopback():
    assert serve_pair_link("127.0.0.1", 47100, "secret", "ws") is None


def test_serve_pair_link_concrete_host_round_trips():
    link = serve_pair_link("192.168.1.50", 47100, "secret", "ws")
    assert link is not None and link.startswith("groundcontrol://add?")
    p = parse_pair_link(link)
    assert p["url"] == "http://192.168.1.50:47100"
    assert p["token"] == "secret"
    assert p["workspace"] == "ws"


def test_serve_pair_link_unspecified_uses_detected_ip():
    link = serve_pair_link("0.0.0.0", 47100, "secret", "ws", primary_ip=lambda: "100.1.2.3")
    assert parse_pair_link(link)["url"] == "http://100.1.2.3:47100"
    assert serve_pair_link("0.0.0.0", 47100, "secret", "ws", primary_ip=lambda: None) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/core/test_serve_pair.py -q`
Expected: FAIL — `mship.core.serve_pair` doesn't exist.

- [ ] **Step 3: Implement the module**

Create `src/mship/core/serve_pair.py`:

```python
from __future__ import annotations

import socket

from mship.core.relay.pairing import build_pair_link

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_UNSPECIFIED = {"0.0.0.0", "::"}


def _primary_ipv4() -> str | None:
    """Best-effort primary outbound IPv4. A UDP socket's getsockname yields the
    route's source address WITHOUT sending any packets. None on failure."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def resolve_advertised_host(host: str, primary_ip=None) -> str | None:
    """Address to advertise in a pairing QR, or None when not reachable from a phone.

    concrete non-loopback host -> itself; 0.0.0.0/:: -> best-effort primary IPv4
    (or None); loopback -> None. `primary_ip` (a no-arg callable) is resolved at
    call time so it can be overridden in tests / monkeypatched."""
    if host in _UNSPECIFIED:
        return (primary_ip or _primary_ipv4)()
    if host in _LOOPBACK:
        return None
    return host


def serve_pair_link(
    host: str, port: int, token: str | None, workspace: str, primary_ip=None
) -> str | None:
    """The groundcontrol://add pairing link to print for a non-relay serve, or None
    when not pairable (no token, or no reachable advertised host)."""
    if not token:
        return None
    adv = resolve_advertised_host(host, primary_ip=primary_ip)
    if adv is None:
        return None
    return build_pair_link(url=f"http://{adv}:{port}", token=token, workspace=workspace)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/core/test_serve_pair.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/serve_pair.py tests/core/test_serve_pair.py
git commit -m "feat(serve): serve_pair — advertised-host resolution + non-relay pair link"
mship journal "added serve_pair (resolve_advertised_host, serve_pair_link); pure, tested" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2 (mothership): wire the non-relay serve path to print the QR

**Files:**
- Modify: `src/mship/cli/serve.py`
- Test: `tests/cli/test_serve.py` (append)

Work in the **mothership** worktree.

- [ ] **Step 1: Write the failing tests** — append to `tests/cli/test_serve.py` (it already has `runner`, the `_configured` fixture, and the `monkeypatch uvicorn.run` pattern):

```python
def test_serve_prints_pair_link_with_token_and_concrete_host(_configured, monkeypatch):
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "secret")
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    result = runner.invoke(app, ["serve", "--host", "192.168.1.50"])
    assert result.exit_code == 0, result.output
    assert "groundcontrol://add?" in result.output
    assert "192.168.1.50" in result.output


def test_serve_pair_link_uses_detected_ip_for_bind_all(_configured, monkeypatch):
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "secret")
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr("mship.core.serve_pair._primary_ipv4", lambda: "100.1.2.3")
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 0, result.output
    assert "http://100.1.2.3" in result.output


def test_serve_no_pair_link_without_token(_configured, monkeypatch):
    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    result = runner.invoke(app, ["serve"])  # loopback default, no token
    assert result.exit_code == 0, result.output
    assert "groundcontrol://add" not in result.output
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/cli/test_serve.py -q`
Expected: the two new "prints pair link" tests FAIL (no pair link emitted yet); `test_serve_no_pair_link_without_token` may pass already.

- [ ] **Step 3: Wire it in `src/mship/cli/serve.py`**

In the non-relay branch, find:
```python
        output.print(f"mship serve → http://{host}:{port}  ({auth_note}; {docs_note})")
        uvicorn.run(api, host=host, port=port)
```
and replace it with:
```python
        output.print(f"mship serve → http://{host}:{port}  ({auth_note}; {docs_note})")

        from mship.core.serve_pair import serve_pair_link
        pair = serve_pair_link(host, port, token, config.workspace)
        if pair is not None:
            import segno
            output.print(f"pair → {pair}")
            output.print(
                "  plain HTTP — fine on a trusted LAN or tailnet (WireGuard-encrypted); "
                "use --relay for untrusted networks"
            )
            typer.echo(segno.make(pair).terminal(compact=True))
        elif token and host in {"0.0.0.0", "::"}:
            output.print(
                "  (couldn't determine a LAN/tailnet IP for a pairing QR; "
                "pass --host <your-ip> to print one)"
            )

        uvicorn.run(api, host=host, port=port)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/cli/test_serve.py -q`
Expected: PASS (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/serve.py tests/cli/test_serve.py
git commit -m "feat(serve): print a pairing QR + advisory for non-relay serve with a token"
mship journal "non-relay serve now prints the groundcontrol://add QR + plain-HTTP advisory; CLI tests green" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3 (ground-control): guard test that an http pair link parses

**Files:**
- Modify: `android/app/src/test/java/com/atomikpanda/groundcontrol/PairLinkTest.kt`

Work in the **ground-control** worktree (`.worktrees/non-relay-qr-pairing/ground-control`). NO functional source change — the scanner (`SettingsViewModel.addFromLink` → `PairLink.parse`) and deep-link handler already pair from this link; `PairLink.parse` stores `url` verbatim with no https requirement. This test locks that in (the existing cases only cover https).

- [ ] **Step 1: Add the test** — append inside the `PairLinkTest` class:

```kotlin
    @Test fun parses_http_lan_link() {
        val c = PairLink.parse(
            "groundcontrol://add?url=http%3A%2F%2F192.168.1.50%3A47100&token=secret&workspace=home"
        )
        assertEquals("http://192.168.1.50:47100", c!!.baseUrl)
        assertEquals("secret", c.token)
        assertEquals("home", c.workspaceName)
    }
```

- [ ] **Step 2: Run it**

Run: `source ~/toolchains/android-env.sh && cd android && ./gradlew testDebugUnitTest --tests "com.atomikpanda.groundcontrol.PairLinkTest"`
Expected: PASS (existing + 1 new).

- [ ] **Step 3: Commit**

```bash
git add android/app/src/test/java/com/atomikpanda/groundcontrol/PairLinkTest.kt
git commit -m "test(gc): PairLink parses an http LAN/tailnet pair link (non-relay pairing)"
mship journal "added PairLink http-link guard test (non-relay pairing); green" --action committed --repo ground-control
```
<!-- /mship:task -->

<!-- mship:task id=4 -->
### Task 4: full verification + phase transition

**Files:** none.

- [ ] **Step 1: mothership suite** (from the mothership worktree)

Run: `uv run pytest tests/core/test_serve_pair.py tests/cli/test_serve.py -q` then the full `uv run pytest -q`.
Expected: green, no regressions.

- [ ] **Step 2: ground-control suite** (from the ground-control worktree)

Run: `mship test` (or `source ~/toolchains/android-env.sh && cd android && ./gradlew testDebugUnitTest`).
Expected: green.

- [ ] **Step 3: Confirm acceptance criteria**

Re-read `specs/2026-06-22-non-relay-qr-pairing.md`: ac1 (QR + link emitted) → Task 2; ac2 (host resolution pure + tested) → Task 1; ac3 (no token → none; relay unchanged) → Tasks 1/2; ac4 (advisory line) → Task 2; ac5 (GC http parse) → Task 3; ac6 (tests, no uvicorn) → Tasks 1/2/3. Note any gap.

- [ ] **Step 4: Journal + transition**

```bash
mship journal "non-relay QR pairing implemented across mothership + ground-control; suites green" --action completed --test-state pass
mship phase review
```

> Then `mship finish --body-file <path>` (real Summary + Test plan) to open the PR(s).
<!-- /mship:task -->

---

## Non-goals (from the spec)

TLS / self-signed certs / cert-pinning (separate future spec) · a `--qr`/`--no-qr` flag (automatic, like the relay) · multi-interface IP enumeration · any functional ground-control change · changing the relay path.
