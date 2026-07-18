"""Tests for the relay-facing CLI: `mship pair`, `mship relay setup`,
and the `mship serve --relay` wiring (Task B7)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def _override(config_path: Path, state_dir: Path):
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(config_path)
    container.state_dir.override(state_dir)


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()


@pytest.fixture
def relay_configured_workspace(workspace: Path, monkeypatch):
    """The standard `workspace` fixture + a `relay:` block + a seeded serve token."""
    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)

    config = workspace / "mothership.yaml"
    # Append a relay block and rename the workspace so subdomain slugging is exercised.
    text = config.read_text().replace(
        "workspace: test-platform",
        "workspace: Mship Workspace\n\nrelay:\n  host: relay.example.com\n  ssh_port: 2222\n  user: tunnel",
    )
    config.write_text(text)

    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    # Seed a serve token so `mship pair` output is deterministic.
    (state_dir / "serve-token").write_text("seeded-token-value\n")

    _override(config, state_dir)
    yield workspace
    _reset()


@pytest.fixture
def workspace_no_relay(workspace: Path, monkeypatch):
    """The standard `workspace` fixture with NO `relay:` block — `pair` errors cleanly."""
    monkeypatch.delenv("MSHIP_SERVE_TOKEN", raising=False)
    config = workspace / "mothership.yaml"
    state_dir = workspace / ".mothership"
    state_dir.mkdir(exist_ok=True)
    _override(config, state_dir)
    yield workspace
    _reset()


# --- mship pair ---


def test_pair_outputs_deeplink(relay_configured_workspace):
    r = runner.invoke(app, ["pair"])
    assert r.exit_code == 0, r.output
    assert "groundcontrol://add?" in r.output
    assert "workspace=" in r.output


def test_pair_url_uses_subdomain_and_relay_host(relay_configured_workspace, tmp_path, monkeypatch):
    """The deep-link url is https://<per-device-subdomain>.<relay-host>, percent-encoded."""
    from mship.core.relay.tunnel import device_id, device_subdomain
    from mship.core.relay.keys import ensure_subdomain_secret, relay_public_key

    # Pre-create the relay key under a fake HOME so no ssh-keygen subprocess runs.
    fake_home = tmp_path / "home"
    (fake_home / ".mothership").mkdir(parents=True)
    key = fake_home / ".mothership" / "relay_ed25519"
    key.write_text("PRIV\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Compute the expected per-device subdomain from the same stub key material.
    secret = ensure_subdomain_secret(home=fake_home)
    expected_subdomain = device_subdomain("Mship Workspace", device_id(relay_public_key(key)), secret)
    expected_url_fragment = f"{expected_subdomain}.relay.example.com"

    r = runner.invoke(app, ["pair"])
    assert r.exit_code == 0, r.output
    # The per-device subdomain (not the old name-only slug) appears in the deep-link URL.
    assert expected_url_fragment in r.output
    # the seeded token rides along
    assert "seeded-token-value" in r.output


def test_pair_errors_without_relay(workspace_no_relay):
    r = runner.invoke(app, ["pair"])
    assert r.exit_code != 0
    assert "relay" in r.output.lower()


def test_pair_with_relay_host_flag_no_block(workspace_no_relay, tmp_path, monkeypatch):
    """ac1: --relay-host prints a valid link with NO relay: block (no 'No relay
    configured' exit)."""
    fake_home = tmp_path / "home"
    (fake_home / ".mothership").mkdir(parents=True)
    key = fake_home / ".mothership" / "relay_ed25519"
    key.write_text("PRIV\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    (workspace_no_relay / ".mothership" / "serve-token").write_text("flag-token\n")

    r = runner.invoke(app, ["pair", "--relay-host", "flag.relay.com"])
    assert r.exit_code == 0, r.output
    assert "groundcontrol://add?" in r.output
    assert "flag.relay.com" in r.output
    assert "flag-token" in r.output


def test_pair_flag_overrides_config_and_record(relay_configured_workspace, tmp_path, monkeypatch):
    """ac6: --relay-host wins over config.relay.host AND a live runtime record."""
    import os

    from mship.core.relay.runtime import RelayRuntimeRecord, write_runtime_record

    fake_home = tmp_path / "home"
    (fake_home / ".mothership").mkdir(parents=True)
    key = fake_home / ".mothership" / "relay_ed25519"
    key.write_text("PRIV\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    write_runtime_record(
        relay_configured_workspace,
        RelayRuntimeRecord(host="record.relay.com", pid=os.getpid()),
    )

    r = runner.invoke(app, ["pair", "--relay-host", "flag.relay.com"])
    assert r.exit_code == 0, r.output
    assert "flag.relay.com" in r.output
    assert "relay.example.com" not in r.output   # config host lost
    assert "record.relay.com" not in r.output    # record host lost


def test_pair_no_relay_error_names_flag_and_serve(workspace_no_relay):
    """ac5: nothing resolves → non-zero + actionable message naming --relay-host +
    serve; never a partial/empty link."""
    r = runner.invoke(app, ["pair"])
    assert r.exit_code != 0
    assert "--relay-host" in r.output
    assert "serve" in r.output.lower()
    assert "groundcontrol://add" not in r.output


def test_pair_autodiscovers_live_serve_record_byte_for_byte(workspace_no_relay, tmp_path, monkeypatch):
    """ac2/ac3/ac4: bare `mship pair` with NO relay: block discovers the live serve
    record's host and prints the SAME link the serve builder produces, same token."""
    import os

    from mship.core.relay.link import build_relay_pair_link
    from mship.core.relay.pairing import parse_pair_link
    from mship.core.relay.runtime import RelayRuntimeRecord, write_runtime_record
    from mship.core.relay.token import ensure_serve_token

    fake_home = tmp_path / "home"
    (fake_home / ".mothership").mkdir(parents=True)
    key = fake_home / ".mothership" / "relay_ed25519"
    key.write_text("PRIV\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    ws = workspace_no_relay
    (ws / ".mothership" / "serve-token").write_text("shared-serve-token\n")

    # The serve path: exactly what a running serve builds + prints (Task 5).
    serve_side = build_relay_pair_link(
        workspace="test-platform",
        host="mship-relay.atomikpanda.com",
        workspace_root=ws,
        home=fake_home,
    )

    # Simulate the running serve having persisted its runtime record (pid alive).
    write_runtime_record(
        ws, RelayRuntimeRecord(host="mship-relay.atomikpanda.com", pid=os.getpid())
    )

    # The pair path: no flag, no relay: block → auto-discovery.
    r = runner.invoke(app, ["pair"])
    assert r.exit_code == 0, r.output

    # ac3: byte-for-byte identical link.
    assert serve_side.link in r.output
    # ac4: token equals ensure_serve_token (the serve's token source).
    assert serve_side.token == ensure_serve_token(ws) == "shared-serve-token"

    p = parse_pair_link(serve_side.link)
    assert p["url"] == serve_side.url
    assert p["token"] == "shared-serve-token"
    assert p["workspace"] == "test-platform"


def test_pair_ignores_stale_record(workspace_no_relay, monkeypatch):
    """ac7: a record whose pid is dead is ignored — pair does NOT print a link
    derived from it and falls through to the clear error (no relay: block, no flag)."""
    from mship.core.relay.runtime import RelayRuntimeRecord, write_runtime_record

    write_runtime_record(
        workspace_no_relay, RelayRuntimeRecord(host="dead.relay.com", pid=424242)
    )
    # Force the record's pid to read as dead, deterministically.
    monkeypatch.setattr("mship.core.relay.runtime._pid_alive", lambda pid: False)

    r = runner.invoke(app, ["pair"])
    assert r.exit_code != 0
    assert "dead.relay.com" not in r.output          # never a stale link
    assert "groundcontrol://add" not in r.output
    assert "--relay-host" in r.output                # actionable fallback message


def test_pair_relay_block_unchanged_ignores_record(relay_configured_workspace, tmp_path, monkeypatch):
    """ac8: with a relay: block and no flag, `mship pair` behaves exactly as today —
    it uses config.relay.host, prints the same explanatory note, and ignores any live
    serve record (config > record)."""
    import os

    from mship.core.relay.link import build_relay_pair_link
    from mship.core.relay.runtime import RelayRuntimeRecord, write_runtime_record

    fake_home = tmp_path / "home"
    (fake_home / ".mothership").mkdir(parents=True)
    key = fake_home / ".mothership" / "relay_ed25519"
    key.write_text("PRIV\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # A live record with a DIFFERENT host must NOT override the relay: block.
    write_runtime_record(
        relay_configured_workspace,
        RelayRuntimeRecord(host="other.relay.com", pid=os.getpid()),
    )

    expected = build_relay_pair_link(
        workspace="Mship Workspace",
        host="relay.example.com",  # the configured block host
        workspace_root=relay_configured_workspace,
        home=fake_home,
    )

    r = runner.invoke(app, ["pair"])
    assert r.exit_code == 0, r.output
    assert expected.link in r.output            # exactly today's link
    assert "other.relay.com" not in r.output    # record ignored (config wins)
    assert "opaque subdomain" in r.output       # the same explanatory note as today


# --- mship relay setup ---


def test_relay_setup_prints_public_key(tmp_path, monkeypatch):
    """`mship relay setup` prints the relay public key (ssh-ed25519 line).

    We point HOME at a tmp dir and pre-create the key so no real `ssh-keygen`
    subprocess runs in the test.
    """
    fake_home = tmp_path / "home"
    key_dir = fake_home / ".mothership"
    key_dir.mkdir(parents=True)
    key = key_dir / "relay_ed25519"
    key.write_text("PRIVATE-KEY-MATERIAL\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 mship-relay\n")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    r = runner.invoke(app, ["relay", "setup"])
    assert r.exit_code == 0, r.output
    assert "ssh-ed25519 " in r.output
    assert "pubkeys" in r.output  # the allow-list instruction
    assert "scp " in r.output  # a ready-to-run remote enroll command, not just prose
    assert "relay host itself" in r.output.lower()  # the local-copy shortcut (no ssh on the box)
    assert "restart the relay" not in r.output  # the misleading step is gone
    assert "no restart needed" in r.output.lower()  # sish reloads keys per connection


def test_relay_setup_generates_key_when_absent(tmp_path, monkeypatch):
    """When no key exists, `relay setup` runs the key generator and prints the pubkey.

    No real `ssh-keygen` runs: we patch the keys module's subprocess.run (which the
    default runner calls) with a fake that writes a stub key + pub file.
    """
    fake_home = tmp_path / "home2"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    import mship.core.relay.keys as keys_mod

    class _CompletedStub:
        returncode = 0

    def fake_run(argv, *args, **kwargs):
        # ssh-keygen -t ed25519 -f <path> -N "" -C mship-relay
        f_idx = argv.index("-f")
        key_path = Path(argv[f_idx + 1])
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("PRIV\n")
        (Path(str(key_path) + ".pub")).write_text("ssh-ed25519 GENERATED mship-relay\n")
        return _CompletedStub()

    monkeypatch.setattr(keys_mod.subprocess, "run", fake_run)

    r = runner.invoke(app, ["relay", "setup"])
    assert r.exit_code == 0, r.output
    assert "ssh-ed25519 GENERATED" in r.output


# --- mship serve --relay (wiring) ---


def test_serve_relay_wires_tunnel_and_loopback(relay_configured_workspace, tmp_path, monkeypatch):
    """`serve --relay` binds uvicorn to loopback, starts+stops a supervised ssh
    tunnel, requires the token, and prints the public URL + pairing QR.

    No real uvicorn or ssh runs: uvicorn.run and TunnelSupervisor are faked.
    """
    from mship.core.relay.tunnel import device_id, device_subdomain
    from mship.core.relay.keys import ensure_subdomain_secret, relay_public_key

    # Pre-create the relay key under a fake HOME so no ssh-keygen subprocess runs.
    fake_home = tmp_path / "home"
    (fake_home / ".mothership").mkdir(parents=True)
    key = fake_home / ".mothership" / "relay_ed25519"
    key.write_text("PRIV\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Compute the expected per-device subdomain from the same stub key material.
    secret = ensure_subdomain_secret(home=fake_home)
    expected_subdomain = device_subdomain("Mship Workspace", device_id(relay_public_key(key)), secret)
    expected_public_url = f"https://{expected_subdomain}.relay.example.com"

    seen: dict = {}
    fake_sup = MagicMock()

    def fake_supervisor_cls(*args, **kwargs):
        seen["argv"] = kwargs.get("argv")
        return fake_sup

    def fake_uvicorn_run(api, host, port):
        seen["uvicorn_host"] = host
        seen["uvicorn_port"] = port
        seen["start_called_before_run"] = fake_sup.start.called

    with patch("uvicorn.run", fake_uvicorn_run), \
         patch("mship.core.relay.tunnel.TunnelSupervisor", fake_supervisor_cls):
        r = runner.invoke(
            app,
            ["serve", "--relay", "--port", "47100", "--relay-tick", "0.01"],
            catch_exceptions=False,
        )

    assert r.exit_code == 0, r.output
    # uvicorn stays on loopback even though we're relaying.
    assert seen["uvicorn_host"] == "127.0.0.1"
    assert seen["uvicorn_port"] == 47100
    # Tunnel started before serving, torn down after (finally → stop()).
    assert seen["start_called_before_run"] is True
    assert fake_sup.stop.called is True
    # The supervised command is an ssh reverse tunnel to the relay host.
    argv = seen["argv"]
    assert argv[0] == "ssh"
    assert "tunnel@relay.example.com" in argv
    # The per-device subdomain is used in the tunnel forward spec.
    assert any(f"{expected_subdomain}:80:localhost:47100" in a for a in argv)
    # Output advertises the public URL + scannable deep-link.
    assert expected_public_url in r.output
    assert "groundcontrol://add?" in r.output


def test_serve_relay_prints_shared_builder_link(relay_configured_workspace, tmp_path, monkeypatch):
    """ac3 (serve leg): `serve --relay` prints EXACTLY build_relay_pair_link(...).link,
    so the serve and pair paths are the same builder — byte-for-byte identical."""
    from mship.core.relay.link import build_relay_pair_link

    fake_home = tmp_path / "home"
    (fake_home / ".mothership").mkdir(parents=True)
    key = fake_home / ".mothership" / "relay_ed25519"
    key.write_text("PRIV\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    workspace_root = relay_configured_workspace  # config parent; seeds serve-token
    expected = build_relay_pair_link(
        workspace="Mship Workspace",
        host="relay.example.com",
        workspace_root=workspace_root,
        home=fake_home,
    )

    fake_sup = MagicMock()
    with patch("uvicorn.run", lambda *a, **k: None), \
         patch("mship.core.relay.tunnel.TunnelSupervisor", lambda *a, **k: fake_sup):
        r = runner.invoke(
            app,
            ["serve", "--relay", "--port", "47100", "--relay-tick", "0.01"],
            catch_exceptions=False,
        )

    assert r.exit_code == 0, r.output
    assert expected.link in r.output


def test_relay_whoami_matches_known_workspace(tmp_path, monkeypatch):
    """`relay whoami` recovers the workspace by recomputing the opaque slug over
    candidate names on this machine; unrelated subdomains report no match."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from mship.core.relay.keys import ensure_subdomain_secret
    from mship.core.relay.tunnel import device_subdomain, opaque_slug

    secret = ensure_subdomain_secret(home=tmp_path)
    sub = device_subdomain("ground-control", "abc123", secret)

    r = runner.invoke(
        app, ["relay", "whoami", sub, "--workspace", "ground-control", "--workspace", "other"]
    )
    assert r.exit_code == 0, r.output
    assert "ground-control" in r.output

    r2 = runner.invoke(app, ["relay", "whoami", "zzzzzzzz-abc123", "--workspace", "ground-control"])
    assert r2.exit_code == 0, r2.output
    assert "no match" in r2.output.lower()

    # A bare slug (no -<devid> suffix) also resolves — the whole label is a candidate.
    r3 = runner.invoke(
        app, ["relay", "whoami", opaque_slug("ground-control", secret), "--workspace", "ground-control"]
    )
    assert r3.exit_code == 0, r3.output
    assert "ground-control" in r3.output


def test_serve_relay_requires_host(workspace_no_relay, tmp_path, monkeypatch):
    """`--relay` with no configured relay block and no --relay-host errors cleanly."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    r = runner.invoke(app, ["serve", "--relay"])
    assert r.exit_code != 0
    assert "relay" in r.output.lower()


# --- mship relay requests / approve / deny ---


def test_relay_requests_approve_deny_roundtrip(tmp_path):
    """Host-side roundtrip: create a pending request, list it, approve it.

    Flags are per-command (not on the relay group), so --store-dir/--pubkeys-dir
    are passed after the subcommand name.
    """
    from mship.core.relay.enroll import RequestStore

    store_dir = tmp_path / "store"
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()

    # Seed a pending request directly via the store (simulates a device POSTing /enroll).
    s = RequestStore(store_dir)
    rid = s.create(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAA host",
        "laptop",
    )

    # `mship relay requests --store-dir <dir>` should list the pending request.
    r = runner.invoke(app, ["relay", "requests", "--store-dir", str(store_dir)])
    assert r.exit_code == 0, r.output
    assert rid in r.output
    assert "laptop" in r.output

    # `mship relay approve <id> --store-dir <dir> --pubkeys-dir <dir>` should
    # write the key into pubkeys/ and mark the request as approved.
    r = runner.invoke(
        app,
        ["relay", "approve", rid, "--store-dir", str(store_dir), "--pubkeys-dir", str(pubkeys)],
    )
    assert r.exit_code == 0, r.output
    assert len(list(pubkeys.glob("*.pub"))) == 1  # key enrolled
    assert RequestStore(store_dir).get(rid) == "approved"


def test_relay_deny_resolves_without_enrolling(tmp_path):
    """deny removes the request from pending but writes no key file."""
    from mship.core.relay.enroll import RequestStore

    store_dir = tmp_path / "store"
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()

    s = RequestStore(store_dir)
    rid = s.create(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAA host",
        "phone",
    )

    r = runner.invoke(app, ["relay", "deny", rid, "--store-dir", str(store_dir)])
    assert r.exit_code == 0, r.output
    assert len(list(pubkeys.glob("*.pub"))) == 0  # no key enrolled
    assert RequestStore(store_dir).get(rid) == "denied"


def test_relay_approve_unknown_id_exits_nonzero(tmp_path):
    """approve on a non-existent id exits 1 with a clean error (no traceback)."""
    store_dir = tmp_path / "store"
    pubkeys = tmp_path / "pubkeys"
    pubkeys.mkdir()

    r = runner.invoke(
        app,
        ["relay", "approve", "doesnotexist", "--store-dir", str(store_dir), "--pubkeys-dir", str(pubkeys)],
    )
    assert r.exit_code == 1
    # Output should mention the id, not a Python traceback.
    assert "doesnotexist" in r.output


def test_relay_deny_unknown_id_exits_nonzero(tmp_path):
    """deny on a non-existent id exits 1 with a clean error (no traceback)."""
    store_dir = tmp_path / "store"

    r = runner.invoke(app, ["relay", "deny", "nope", "--store-dir", str(store_dir)])
    assert r.exit_code == 1
    assert "nope" in r.output


def test_relay_requests_empty(tmp_path):
    """`mship relay requests` with no pending requests exits 0 and says so."""
    store_dir = tmp_path / "store"

    r = runner.invoke(app, ["relay", "requests", "--store-dir", str(store_dir)])
    assert r.exit_code == 0, r.output
    assert "no pending" in r.output


# --- mship relay enroll (requester) ---


def _stub_relay_key(tmp_path, monkeypatch):
    """Point HOME at a tmp dir with a pre-created relay key so `enroll` reads a
    real pubkey without spawning ssh-keygen."""
    fake_home = tmp_path / "home"
    (fake_home / ".mothership").mkdir(parents=True)
    key = fake_home / ".mothership" / "relay_ed25519"
    key.write_text("PRIV\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))


def test_enroll_post_connection_error_is_clean(tmp_path, monkeypatch):
    """A connection error on the initial POST exits 1 with a clean message, not a traceback."""
    import httpx

    _stub_relay_key(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", boom)

    r = runner.invoke(app, ["relay", "enroll", "--enroll-url", "http://relay.example:47180"])
    assert r.exit_code == 1
    assert "could not reach enroll server" in r.output
    # No traceback leaked.
    assert "Traceback" not in r.output


def test_enroll_poll_survives_transient_blip_then_approved(tmp_path, monkeypatch):
    """A transient RequestError mid-poll is retried; the loop then sees 'approved' and exits 0."""
    import httpx

    _stub_relay_key(tmp_path, monkeypatch)

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp({"id": "rid123", "status": "pending"}))

    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("blip")  # transient — should be retried
        return _Resp({"status": "approved"})

    monkeypatch.setattr(httpx, "get", fake_get)
    # No real sleeping between poll iterations.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)

    r = runner.invoke(app, ["relay", "enroll", "--enroll-url", "http://relay.example:47180"])
    assert r.exit_code == 0, r.output
    assert "approved" in r.output
    assert calls["n"] == 2  # first call blipped, second succeeded


def test_enroll_poll_denied_exits_nonzero(tmp_path, monkeypatch):
    """A 'denied' status during polling exits 1 cleanly."""
    import httpx

    _stub_relay_key(tmp_path, monkeypatch)

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp({"id": "rid123", "status": "pending"}))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp({"status": "denied"}))
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)

    r = runner.invoke(app, ["relay", "enroll", "--enroll-url", "http://relay.example:47180"])
    assert r.exit_code == 1
    assert "denied" in r.output


def test_enroll_no_wait_returns_after_post(tmp_path, monkeypatch):
    """--no-wait POSTs and returns 0 without polling."""
    import httpx

    _stub_relay_key(tmp_path, monkeypatch)

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": "rid123", "status": "pending"}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())

    def fail_get(*a, **k):
        raise AssertionError("should not poll when --no-wait")

    monkeypatch.setattr(httpx, "get", fail_get)

    r = runner.invoke(
        app, ["relay", "enroll", "--enroll-url", "http://relay.example:47180", "--no-wait"]
    )
    assert r.exit_code == 0, r.output
    assert "rid123" in r.output
