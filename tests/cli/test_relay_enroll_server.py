"""Tests for `mship relay enroll-server` CLI command (Task 3).

Verifies:
  - loopback (127.0.0.1) is the default bind host
  - --relay-domain flag is passed through to build_enroll_app
  - RELAY_DOMAIN env var provides the default when --relay-domain is omitted

These tests use module-level monkeypatching seams (_run_uvicorn, build_enroll_app)
so no real server is started.
"""
from __future__ import annotations

import typer
from typer.testing import CliRunner

import mship.cli.relay as relay_mod
import mship.core.relay.enroll_app as ea


def _app(monkeypatch):
    captured = {}
    monkeypatch.setattr(relay_mod, "_run_uvicorn",
                        lambda app, host, port: captured.update(host=host, port=port, app=app))
    monkeypatch.setattr(ea, "build_enroll_app",
                        lambda store, *, relay_domain: captured.update(relay_domain=relay_domain) or "APP")
    app = typer.Typer()
    relay_mod.register(app, lambda: None)
    return app, captured


def test_enroll_server_defaults_to_loopback(monkeypatch, tmp_path):
    app, captured = _app(monkeypatch)
    res = CliRunner().invoke(app, ["relay", "enroll-server",
                                   "--store-dir", str(tmp_path / "s"),
                                   "--pubkeys-dir", str(tmp_path / "p"),
                                   "--relay-domain", "r.example.com"])
    assert res.exit_code == 0, res.output
    assert captured["host"] == "127.0.0.1"          # loopback default (ac5)
    assert captured["relay_domain"] == "r.example.com"


def test_enroll_server_relay_domain_defaults_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_DOMAIN", "env.example.com")
    app, captured = _app(monkeypatch)
    res = CliRunner().invoke(app, ["relay", "enroll-server",
                                   "--store-dir", str(tmp_path / "s"),
                                   "--pubkeys-dir", str(tmp_path / "p")])
    assert res.exit_code == 0, res.output
    assert captured["relay_domain"] == "env.example.com"
