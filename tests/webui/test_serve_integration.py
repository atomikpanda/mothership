"""The console as serve actually mounts it: one coupling point, detachable,
behind the same header bearer."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mship.core.serve import create_app


class Cfg:
    workspace = "ws"
    run_hosts = ()
    repos = {}
    relay = None
    spec_storage = "committed"


@pytest.fixture(autouse=True)
def _no_pr_watch(monkeypatch):
    monkeypatch.setenv("MSHIP_PR_WATCH_INTERVAL", "0")


@pytest.fixture
def app_factory(tmp_path):
    def build(*, config=None, token="tok"):
        specs = tmp_path / "specs"
        specs.mkdir(exist_ok=True)
        (tmp_path / ".mothership").mkdir(exist_ok=True)

        class _State:
            def load(self):
                class S:
                    tasks = {}
                return S()

        return create_app(
            specs_dir=specs, state_manager=_State(), log_manager=None,
            workspace_root=tmp_path, workspace_name="ws", auth_token=token,
            config=config if config is not None else Cfg(),
        )
    return build


def test_serve_renders_the_console_from_the_real_topology(app_factory):
    with TestClient(app_factory()) as client:
        r = client.get("/ui", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # a real probe ran: the run-hosts aggregate edge is always present
    assert "run_hosts" in r.text


def test_the_console_requires_the_header_bearer(app_factory):
    """ac4: auth is a bearer in a HEADER, not a cookie/session — so a future
    different-origin frontend authenticates against the same surface unchanged."""
    with TestClient(app_factory()) as client:
        assert client.get("/ui").status_code == 401
        r = client.get("/ui", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    # no session cookie was set: nothing to be same-origin-bound to
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_the_console_and_the_endpoint_agree(app_factory):
    """One payload builder feeds both, so the page cannot drift from the JSON."""
    with TestClient(app_factory()) as client:
        auth = {"Authorization": "Bearer tok"}
        payload = client.get("/net/topology", headers=auth).json()
        html = client.get("/ui", headers=auth).text
    for edge in payload["edges"]:
        assert edge["name"] in html, f"{edge['name']} in the JSON but not the page"
    assert payload["mship_version"] in html


def test_serve_has_exactly_one_ui_coupling_point():
    """ac1: serve.py's only UI-specific code is the mount registration."""
    import mship.core.serve as serve_mod

    source = Path(serve_mod.__file__).read_text()
    assert source.count("mount_webui") == 2, "expected exactly an import and one call"
    for leaked in (".html", "app.css", "templates", '"/ui'):
        assert leaked not in source, f"UI detail {leaked!r} leaked into serve.py"


def test_serve_still_works_with_the_frontend_absent(app_factory, monkeypatch):
    """ac2: the frontend is detachable — serve degrades, it does not break."""
    import builtins

    real_import = builtins.__import__

    def no_webui(name, *args, **kw):
        if name.startswith("mship.webui"):
            raise ImportError("frontend package removed")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", no_webui)

    with TestClient(app_factory()) as client:
        auth = {"Authorization": "Bearer tok"}
        assert client.get("/health", headers=auth).status_code == 200
        assert client.get("/net/topology", headers=auth).status_code == 200
        assert client.get("/ui", headers=auth).status_code == 404


def test_no_console_without_a_workspace_config(tmp_path):
    """With no config there is no topology to render, so the console is absent
    rather than rendering an empty shell — matching how /net/topology 503s."""
    specs = tmp_path / "specs"
    specs.mkdir()
    (tmp_path / ".mothership").mkdir()

    class _State:
        def load(self):
            class S:
                tasks = {}
            return S()

    app = create_app(
        specs_dir=specs, state_manager=_State(), log_manager=None,
        workspace_root=tmp_path, workspace_name="ws", auth_token="tok",
        config=None,
    )
    with TestClient(app) as client:
        auth = {"Authorization": "Bearer tok"}
        assert client.get("/ui", headers=auth).status_code == 404
        assert client.get("/net/topology", headers=auth).status_code == 503
