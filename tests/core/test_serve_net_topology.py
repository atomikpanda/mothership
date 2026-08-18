import pytest
from fastapi.testclient import TestClient

from mship.core.serve import create_app


class Cfg:
    workspace = "ws"
    run_hosts = ()
    repos = {}
    relay = None
    spec_storage = "committed"


@pytest.fixture
def app_factory(tmp_path):
    def build(*, config, token="tok", **kwargs):
        specs = tmp_path / "specs"
        specs.mkdir(exist_ok=True)
        (tmp_path / ".mothership").mkdir(exist_ok=True)

        class _State:
            def load(self):
                class S:
                    tasks = {}
                return S()

        return create_app(
            pr_watch_interval=0,
            specs_dir=specs, state_manager=_State(), log_manager=None,
            workspace_root=tmp_path, workspace_name="ws", auth_token=token,
            config=config, **kwargs,
        )
    return build


def test_requires_the_bearer(app_factory):
    with TestClient(app_factory(config=Cfg())) as client:
        assert client.get("/net/topology").status_code == 401


def test_returns_the_payload_with_a_version(app_factory):
    with TestClient(app_factory(config=Cfg())) as client:
        r = client.get("/net/topology", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert body["workspace"] == "ws"
    assert body["probed_at"]
    assert isinstance(body["edges"], list)


def test_payload_is_renderable_on_its_own(app_factory):
    """AC6: every field the console renders comes from this response — no
    in-process-only data. If the console needs a field, it is asserted here."""
    with TestClient(app_factory(config=Cfg())) as client:
        body = client.get(
            "/net/topology", headers={"Authorization": "Bearer tok"}
        ).json()

    assert set(body) >= {"version", "workspace", "probed_at", "edges"}
    assert body["edges"], "expected at least one edge to render"
    for edge in body["edges"]:
        assert set(edge) == {"kind", "name", "status", "code", "detail", "fix", "facts"}
        assert edge["status"] in ("ok", "warn", "fail", "absent")


def test_503_when_serve_has_no_workspace_config(app_factory):
    with TestClient(app_factory(config=None)) as client:
        r = client.get("/net/topology", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 503
    assert "bootstrap" in r.json()["detail"].lower()


def test_never_500s_on_a_broken_environment(app_factory):
    class Broken(Cfg):
        run_hosts = ("mac", "linux")

    with TestClient(app_factory(config=Broken())) as client:
        r = client.get("/net/topology", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    codes = {e["code"] for e in r.json()["edges"]}
    assert "run_host_unmapped" in codes


def test_loaded_github_app_capability_overrides_absent_environment(
    app_factory, monkeypatch
):
    monkeypatch.delenv("MSHIP_GH_APP_ID", raising=False)
    monkeypatch.delenv("MSHIP_GH_APP_KEY", raising=False)
    app = app_factory(
        config=Cfg(),
        gh_app_id="persisted-app",
        gh_app_key="PRIVATE KEY",
    )

    with TestClient(app) as client:
        payload = client.get(
            "/net/topology",
            headers={"Authorization": "Bearer tok"},
        ).json()

    edge = next(edge for edge in payload["edges"] if edge["kind"] == "gh_auth")
    assert edge["facts"]["serves_app_backed_tokens"] is True
    assert edge["facts"]["app_id_configured"] is True
    assert edge["facts"]["app_key_readable"] is True
    assert "persisted-app" not in str(edge["facts"])
    assert "PRIVATE KEY" not in str(edge["facts"])
