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
    from mship.core.relay.keys import relay_public_key

    # Pre-create the relay key under a fake HOME so no ssh-keygen subprocess runs.
    fake_home = tmp_path / "home"
    (fake_home / ".mothership").mkdir(parents=True)
    key = fake_home / ".mothership" / "relay_ed25519"
    key.write_text("PRIV\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Compute the expected per-device subdomain from the same stub key material.
    expected_subdomain = device_subdomain("Mship Workspace", device_id(relay_public_key(key)))
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
    assert "scp " in r.output  # a ready-to-run enroll command, not just prose
    assert "restart the relay" not in r.output  # the misleading step is gone
    assert "no relay restart needed" in r.output.lower()  # sish reloads keys per connection


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
    from mship.core.relay.keys import relay_public_key

    # Pre-create the relay key under a fake HOME so no ssh-keygen subprocess runs.
    fake_home = tmp_path / "home"
    (fake_home / ".mothership").mkdir(parents=True)
    key = fake_home / ".mothership" / "relay_ed25519"
    key.write_text("PRIV\n")
    (Path(str(key) + ".pub")).write_text("ssh-ed25519 AAAA mship-relay\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Compute the expected per-device subdomain from the same stub key material.
    expected_subdomain = device_subdomain("Mship Workspace", device_id(relay_public_key(key)))
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


def test_serve_relay_requires_host(workspace_no_relay, tmp_path, monkeypatch):
    """`--relay` with no configured relay block and no --relay-host errors cleanly."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    r = runner.invoke(app, ["serve", "--relay"])
    assert r.exit_code != 0
    assert "relay" in r.output.lower()
