"""Relay-owner CLI for the host directory (#471 Task 5).

`mship relay fleet-token` mints the phone's credential and prints the QR that
carries it; `mship relay hosts` is the owner's view of the same directory the
phone reads. Every relay-owner command requires the exact `--store-dir` used by
`enroll-server`; the `pubkeys/` allowlist remains the one sish authenticates
against, making signature-auth and tunnel-auth one identity.
"""

from __future__ import annotations

import json
import re
import time

import pytest
import typer
from typer.testing import CliRunner

import mship.cli.relay as relay_mod
import mship.core.relay.enroll_app as ea
from mship.cli import app as mship_app
from mship.cli.output import reset_output_settings
from mship.core.relay.fleet_token import FleetTokenStore
from mship.core.relay.host_directory import HostDirectory

FP = "SHA256:keyA"


def _app():
    app = typer.Typer()
    relay_mod.register(app, lambda: None)
    return app


def _run(*args):
    return CliRunner().invoke(_app(), ["relay", *args])


def _fleet_token(store_dir, *extra):
    return _run(
        "fleet-token",
        "--store-dir",
        str(store_dir),
        "--relay-domain",
        "relay.example.com",
        *extra,
    )


def _token_line(result):
    """The bare `<label_id>.<secret>` the command printed."""
    lines = (ln.strip() for ln in result.output.splitlines())
    return next(ln for ln in lines if re.fullmatch(r"[0-9a-f]{16}\.[0-9a-f]{64}", ln))


# --- fleet-token ------------------------------------------------------------

def test_fleet_token_requires_an_explicit_store_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    res = _run(
        "fleet-token",
        "--label",
        "phone",
        "--relay-domain",
        "relay.example.com",
    )
    assert res.exit_code != 0
    assert "--store-dir" in res.output
    assert "pgrep -af" in res.output
    assert not (tmp_path / "pending-store").exists()


@pytest.mark.parametrize("store_dir", ["", " \t "], ids=["empty", "whitespace"])
@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        ("requests", ()),
        (
            "fleet-token",
            ("--label", "phone", "--relay-domain", "relay.example.com"),
        ),
    ],
)
def test_owner_commands_reject_blank_store_dirs_without_touching_current_dir(
    monkeypatch, tmp_path, command, arguments, store_dir
):
    from mship.core.relay.enroll import RequestStore

    monkeypatch.chdir(tmp_path)
    request_id = RequestStore(tmp_path).create(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAA host",
        "laptop",
    )
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    res = _run(command, *arguments, "--store-dir", store_dir)

    assert res.exit_code != 0
    assert "--store-dir" in res.output
    assert request_id not in res.output
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    "pubkeys_option",
    [(), ("--pubkeys-dir", ""), ("--pubkeys-dir", " \t ")],
    ids=["missing", "empty", "whitespace"],
)
def test_approve_requires_an_explicit_nonblank_pubkeys_dir(
    monkeypatch, tmp_path, pubkeys_option
):
    from mship.core.relay.enroll import RequestStore

    current_dir = tmp_path / "current"
    current_dir.mkdir()
    store_dir = tmp_path / "store"
    request_id = RequestStore(store_dir).create(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAA host",
        "laptop",
    )
    monkeypatch.chdir(current_dir)

    res = _run("approve", request_id, "--store-dir", str(store_dir), *pubkeys_option)

    assert res.exit_code != 0
    assert "--pubkeys-dir" in res.output
    assert RequestStore(store_dir).get(request_id) == "pending"
    assert list(current_dir.iterdir()) == []


@pytest.mark.parametrize(
    "pubkeys_option",
    [(), ("--pubkeys-dir", ""), ("--pubkeys-dir", " \t ")],
    ids=["missing", "empty", "whitespace"],
)
def test_enroll_server_requires_an_explicit_nonblank_pubkeys_dir(
    monkeypatch, tmp_path, pubkeys_option
):
    started = []
    monkeypatch.setattr(
        relay_mod, "_enroll_server_impl", lambda **kwargs: started.append(kwargs)
    )

    res = _run(
        "enroll-server",
        "--store-dir",
        str(tmp_path / "store"),
        "--relay-domain",
        "relay.example.com",
        *pubkeys_option,
    )

    assert res.exit_code != 0
    assert "--pubkeys-dir" in res.output
    assert not started


def test_fleet_token_mints_and_prints_the_pairing_link(tmp_path):
    res = _fleet_token(tmp_path / "s", "--label", "phone")
    assert res.exit_code == 0, res.output
    token = _token_line(res)
    assert FleetTokenStore(tmp_path / "s").verify(token) == "phone"
    assert "groundcontrol://add-relay?relay=relay.example.com&token=" in res.output


def test_fleet_token_is_stable_across_runs_for_one_label(tmp_path):
    # Re-printing the QR must not invalidate the phone that already scanned it.
    first = _fleet_token(tmp_path / "s", "--label", "phone")
    second = _fleet_token(tmp_path / "s", "--label", "phone")
    assert _token_line(first) == _token_line(second)


def test_fleet_token_mints_a_distinct_token_per_label(tmp_path):
    phone = _token_line(_fleet_token(tmp_path / "s", "--label", "phone"))
    tablet = _token_line(_fleet_token(tmp_path / "s", "--label", "tablet"))
    assert phone != tablet
    store = FleetTokenStore(tmp_path / "s")
    assert (store.verify(phone), store.verify(tablet)) == ("phone", "tablet")


def test_fleet_token_revoke_invalidates_only_that_label(tmp_path):
    phone = _token_line(_fleet_token(tmp_path / "s", "--label", "phone"))
    tablet = _token_line(_fleet_token(tmp_path / "s", "--label", "tablet"))
    res = _fleet_token(tmp_path / "s", "--label", "phone", "--revoke")
    assert res.exit_code == 0, res.output
    store = FleetTokenStore(tmp_path / "s")
    assert store.verify(phone) is None
    assert store.verify(tablet) == "tablet"
    assert "groundcontrol://" not in res.output  # nothing to re-scan


def test_fleet_token_revoking_an_unknown_label_reports_it(tmp_path):
    res = _fleet_token(tmp_path / "s", "--label", "ghost", "--revoke")
    assert res.exit_code == 1
    assert "ghost" in res.output


def test_fleet_token_relay_domain_defaults_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DOMAIN", "env.example.com")
    res = _run("fleet-token", "--store-dir", str(tmp_path / "s"), "--label", "phone")
    assert res.exit_code == 0, res.output
    assert "groundcontrol://add-relay?relay=env.example.com&token=" in res.output


def test_fleet_token_requires_a_relay_domain(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_DOMAIN", raising=False)
    res = _run("fleet-token", "--store-dir", str(tmp_path / "s"), "--label", "phone")
    assert res.exit_code != 0


def test_fleet_token_canonicalizes_the_relay_domain(tmp_path):
    res = _run(
        "fleet-token",
        "--store-dir",
        str(tmp_path / "s"),
        "--label",
        "phone",
        "--relay-domain",
        "  Relay.Example.COM.  ",
    )
    assert res.exit_code == 0, res.output
    assert "groundcontrol://add-relay?relay=relay.example.com&token=" in res.output
    assert "%20" not in res.output


def test_fleet_token_rejects_whitespace_before_writing_a_credential(tmp_path):
    store_dir = tmp_path / "s"
    res = _run(
        "fleet-token",
        "--store-dir",
        str(store_dir),
        "--label",
        "phone",
        "--relay-domain",
        "   ",
    )
    assert res.exit_code != 0
    assert not (store_dir / "fleet-tokens.json").exists()
    assert not (store_dir / "fleet-secret").exists()


# --- hosts ------------------------------------------------------------------


def _seed(store_dir, clock, **over):
    """Land one registered entry through the real directory (no on-disk layout
    knowledge in the test)."""
    directory = HostDirectory(
        store_dir,
        relay_domain="relay.example",
        allowed_signers=lambda: FP,
        probe=lambda url: None,
        verify=lambda *a, **kw: True,
        clock=clock,
    )
    payload = {
        "host_id": "hst-20260818120000-aaaaaaaa",
        "instance_id": "inst-1",
        "label": "vm-alpha",
        "key_fingerprint": FP,
        "machine_fingerprint": "mf",
        "subdomain": "abc123-a1b2c3",
        "public_url": "https://abc123-a1b2c3.relay.example",
        "refresh": "refresh-credential-1",
    }
    payload.update(over)
    directory.register(
        payload, nonce=directory.issue_challenge(FP)["nonce"], signature="s"
    )
    return payload


def test_hosts_json_mode_returns_one_structured_document_without_credentials(tmp_path):
    store_dir = tmp_path / "s"
    now = time.time()
    _seed(store_dir, lambda: now)

    reset_output_settings()
    try:
        res = CliRunner().invoke(
            mship_app,
            ["--json", "relay", "hosts", "--store-dir", str(store_dir)],
        )
    finally:
        reset_output_settings()

    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == {
        "hosts": [
            {
                "host_id": "hst-20260818120000-aaaaaaaa",
                "label": "vm-alpha",
                "state": "online",
                "last_seen": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
                ),
            }
        ]
    }


def test_hosts_lists_id_label_state_and_last_seen(tmp_path):
    store_dir = tmp_path / "s"
    now = time.time()
    _seed(store_dir, lambda: now)
    res = _run("hosts", "--store-dir", str(store_dir))
    assert res.exit_code == 0, res.output
    assert "hst-20260818120000-aaaaaaaa" in res.output
    assert "vm-alpha" in res.output
    assert "online" in res.output  # freshness on the real clock
    assert time.strftime("%Y-%m-%d", time.gmtime(now)) in res.output


def test_hosts_marks_a_host_offline_once_it_stops_beating(tmp_path):
    store_dir = tmp_path / "s"
    _seed(store_dir, lambda: 1_000.0)
    res = _run("hosts", "--store-dir", str(store_dir))
    assert "offline" in res.output


def test_hosts_shows_pending_enrollments_alongside_registered_ones(tmp_path):
    from mship.core.relay.enroll import RequestStore

    store_dir = tmp_path / "s"
    _seed(store_dir, time.time)
    RequestStore(store_dir).create(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISecondKeyBodyBBBBBBBBBBBBBBBBBBBBBBBB two",
        "fresh-vm",
    )
    res = _run("hosts", "--store-dir", str(store_dir))
    assert "pending-approval" in res.output
    assert "fresh-vm" in res.output


def test_hosts_renders_untrusted_labels_as_one_literal_line(tmp_path):
    from mship.core.relay.enroll import RequestStore

    label = "[bold red]trusted[/]\nspoof\x1b[2J"
    RequestStore(tmp_path).create(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISecondKeyBodyBBBBBBBBBBBBBBBBBBBBBBBB two",
        label,
    )

    res = _run("hosts", "--store-dir", str(tmp_path))

    assert res.exit_code == 0, res.output
    assert "[bold red]trusted[/]\\nspoof\\u001b[2J" in res.output
    assert "\x1b" not in res.output


def test_hosts_never_prints_the_refresh_credential(tmp_path):
    # The phone fetches it over `GET /hosts`; the owner's terminal (and its
    # scrollback) is not a place to widen it to.
    store_dir = tmp_path / "s"
    _seed(store_dir, time.time)
    res = _run("hosts", "--store-dir", str(store_dir))
    assert "refresh-credential-1" not in res.output


def test_hosts_on_an_empty_relay_returns_an_empty_json_list(tmp_path):
    res = _run("hosts", "--store-dir", str(tmp_path / "s"))
    assert res.exit_code == 0
    assert json.loads(res.output) == {"hosts": []}


# --- enroll-server wiring ---------------------------------------------------


def test_enroll_server_wires_the_directory_and_the_pubkeys_allowlist(
    monkeypatch, tmp_path
):
    captured = {}
    monkeypatch.setattr(relay_mod, "_run_uvicorn", lambda app, host, port: None)
    monkeypatch.setattr(ea, "build_enroll_app", lambda store, **kw: captured.update(kw))

    pubkeys = tmp_path / "p"
    pubkeys.mkdir()
    (pubkeys / "vm.pub").write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAAAAAA host\n"
    )
    res = _run(
        "enroll-server",
        "--store-dir",
        str(tmp_path / "s"),
        "--pubkeys-dir",
        str(pubkeys),
        "--relay-domain",
        "relay.example.com",
    )
    assert res.exit_code == 0, res.output

    assert isinstance(captured["host_directory"], HostDirectory)
    assert isinstance(captured["fleet_tokens"], FleetTokenStore)
    # The allowed-signers callable is re-read per verification and renders the
    # SAME pubkeys/ dir sish authenticates against.
    signers = captured["host_directory"]._allowed_signers()  # noqa: SLF001
    assert signers.startswith("SHA256:")
    assert "ssh-ed25519" in signers


def test_enroll_server_fleet_tokens_share_the_store_dir(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(relay_mod, "_run_uvicorn", lambda app, host, port: None)
    monkeypatch.setattr(ea, "build_enroll_app", lambda store, **kw: captured.update(kw))
    store_dir = tmp_path / "s"
    token = FleetTokenStore(store_dir).issue("phone")

    res = _run(
        "enroll-server",
        "--store-dir",
        str(store_dir),
        "--pubkeys-dir",
        str(tmp_path / "p"),
        "--relay-domain",
        "relay.example.com",
    )
    assert res.exit_code == 0, res.output
    # A token minted by the CLI must verify inside the running server.
    assert captured["fleet_tokens"].verify(token) == "phone"


def test_the_registration_probe_reads_the_incumbents_instance_id():
    from mship.core.relay.host_directory import probe_instance_id

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"instance_id": "inst-9"}

    seen = {}

    def get(url, **kw):
        seen["url"] = url
        return Resp()

    assert probe_instance_id("https://abc123.relay.example", get=get) == "inst-9"
    assert seen["url"] == "https://abc123.relay.example/health"


def test_the_registration_probe_has_no_answer_for_an_unhealthy_incumbent():
    from mship.core.relay.host_directory import probe_instance_id

    class Resp:
        status_code = 502

        @staticmethod
        def json():
            raise AssertionError("must not parse a non-200 body")

    assert probe_instance_id("https://x", get=lambda url, **kw: Resp()) is None
