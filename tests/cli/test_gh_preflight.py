"""`mship gh preflight` — fail-fast broker auth check (Task 5).

Unlike `gh_auth.resolve_token` (which swallows every broker failure and
degrades to "no token"), preflight is STRICT: it must exit non-zero and
name the offending repo on any broker error, and must exit non-zero when
there's no auth configured at all. httpx is mocked via `httpx.MockTransport`
by patching the `httpx.Client` constructor globally (the CLI command builds
its own client internally, so there is no injection seam through the public
`mship gh preflight` surface — this mirrors how `core/gh_auth.py`'s broker
pull is shaped, just exercised from the CLI down).
"""
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def _ws(root: Path) -> Path:
    ws = root / "ws"
    ws.mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\n"
        "repos:\n"
        "  mothership:\n    path: mothership\n    type: service\n"
        "  ground-control:\n    path: ground-control\n    type: service\n"
    )
    (ws / ".mothership").mkdir()
    return ws


def _configure(ws: Path):
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(ws / "mothership.yaml")
    container.state_dir.override(ws / ".mothership")


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()


def _patch_httpx_client(monkeypatch, handler):
    """Make every `httpx.Client(...)` constructed anywhere use MockTransport(handler)."""
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)


@pytest.fixture(autouse=True)
def _clean_gh_env(monkeypatch):
    # Every test controls its own auth env explicitly; strip whatever the
    # ambient shell/CI might have set so tests are deterministic.
    for var in ("MSHIP_GH_BROKER_URL", "MSHIP_SERVE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_preflight_broker_covers_all_repos_exit_zero(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    _configure(ws)
    monkeypatch.setenv("MSHIP_GH_BROKER_URL", "http://broker.example")
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "serve-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer serve-secret"
        return httpx.Response(200, json={
            "token": "brokered-tok",
            "expires_at": "2026-01-01T00:00:00Z",
            "repositories": ["mothership", "ground-control"],
        })

    _patch_httpx_client(monkeypatch, handler)
    try:
        result = runner.invoke(app, ["gh", "preflight"])
        assert result.exit_code == 0, result.output
        assert "mothership" in result.output
        assert "ground-control" in result.output
    finally:
        _reset()


def test_preflight_broker_502_names_uncovered_repo_exit_nonzero(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    _configure(ws)
    monkeypatch.setenv("MSHIP_GH_BROKER_URL", "http://broker.example")
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "serve-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={
            "detail": (
                "gh-app: installation-token mint failed (403) for repos "
                "['mothership', 'ground-control'] — check the App is installed "
                "on each: {\"message\": \"'ground-control' not accessible to installation\"}"
            ),
        })

    _patch_httpx_client(monkeypatch, handler)
    try:
        result = runner.invoke(app, ["gh", "preflight"])
        assert result.exit_code != 0
        assert "ground-control" in result.output
        assert "install" in result.output.lower()
    finally:
        _reset()


def test_preflight_broker_unreachable_exit_nonzero(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    _configure(ws)
    monkeypatch.setenv("MSHIP_GH_BROKER_URL", "http://broker.example")
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "serve-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_httpx_client(monkeypatch, handler)
    try:
        result = runner.invoke(app, ["gh", "preflight"])
        assert result.exit_code != 0
        assert "broker unreachable" in result.output.lower()
        assert "broker.example" in result.output
    finally:
        _reset()


def test_preflight_no_broker_but_token_present_exit_zero(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    _configure(ws)
    monkeypatch.setenv("GH_TOKEN", "gh-env-token")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("broker must not be called when a token is available")

    _patch_httpx_client(monkeypatch, handler)
    try:
        result = runner.invoke(app, ["gh", "preflight"])
        assert result.exit_code == 0, result.output
        assert "auth ok" in result.output.lower()
    finally:
        _reset()


def test_preflight_no_broker_no_token_exit_nonzero(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    _configure(ws)

    try:
        result = runner.invoke(app, ["gh", "preflight"])
        assert result.exit_code != 0
        assert "no" in result.output.lower()
        assert "auth" in result.output.lower()
        assert "configured" in result.output.lower()
    finally:
        _reset()
