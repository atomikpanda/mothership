"""Control app: the daemon's local identity surface. Version is captured at
process start (the upgrade-in-place lie #470 calls out), capabilities are the
#471/#472/#473 seams."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import mship
from mship.core.daemon.control import PROTOCOL, create_control_app, probe_control_socket
from mship.core.daemon.registry import RegistryStore, WorkspaceEntry

STARTED = datetime(2026, 8, 16, 11, 0, 0, tzinfo=timezone.utc)


def _client(version: str = "0.5.52") -> TestClient:
    app = create_control_app(started_at=STARTED, version=version, socket_path="/run/mship/daemon.sock")
    return TestClient(app)


def test_health_payload_shape():
    now = datetime.now(timezone.utc)
    r = _client().get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mship_version"] == "0.5.52"
    assert body["protocol"] == PROTOCOL
    assert body["started_at"] == STARTED.isoformat()
    assert body["socket"] == "/run/mship/daemon.sock"
    assert body["pid"] > 0
    assert body["uptime_s"] >= (now - STARTED).total_seconds() - 5
    assert body["capabilities"] == {
        "serve": False,
        "tunnel": False,
        "registry": False,
        "runner": False,
    }


def test_version_is_captured_at_start_not_reread(monkeypatch):
    client = _client(version="0.5.51")
    monkeypatch.setattr(mship, "__version__", "9.9.9")
    assert client.get("/health").json()["mship_version"] == "0.5.51"


def test_probe_control_socket_never_raises(tmp_path):
    # No daemon on this socket: probe returns None, no exception.
    assert probe_control_socket(tmp_path / "absent.sock") is None


def test_probe_control_socket_returns_payload():
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "ok", "mship_version": "1"}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def get(self, url, **kw):
            return FakeResponse()

        def close(self):
            pass

    payload = probe_control_socket("/some.sock", client_factory=FakeClient)
    assert payload == {"status": "ok", "mship_version": "1"}


def test_probe_control_socket_non_200_is_none():
    class FakeResponse:
        status_code = 500

        def json(self):  # pragma: no cover - not reached
            return {}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def get(self, url, **kw):
            return FakeResponse()

        def close(self):
            pass

    assert probe_control_socket("/some.sock", client_factory=FakeClient) is None


def _registry_store(tmp_path):
    from datetime import datetime, timezone as tz

    store = RegistryStore(tmp_path / "workspaces.json")
    now = datetime(2026, 8, 17, tzinfo=tz.utc)
    store.mutate(lambda s: s.entries.append(WorkspaceEntry(
        id="ws-1", name="a", path="/w/a", config_path="/w/a/mothership.yaml",
        first_seen=now, last_seen=now,
    )))
    return store


def test_registry_capability_flips_with_store(tmp_path):
    app = create_control_app(started_at=STARTED, version="1", socket_path="/s",
                             store=_registry_store(tmp_path), serve_bound=True)
    caps = TestClient(app).get("/health").json()["capabilities"]
    assert caps["registry"] is True
    assert caps["serve"] is True
    assert caps["tunnel"] is False and caps["runner"] is False


def test_control_workspaces_endpoints(tmp_path):
    calls = []
    app = create_control_app(started_at=STARTED, version="1", socket_path="/s",
                             store=_registry_store(tmp_path), rescan=lambda: calls.append(1))
    client = TestClient(app)
    ws = client.get("/workspaces").json()["workspaces"]
    assert [w["id"] for w in ws] == ["ws-1"]
    assert ws[0]["repos"] == []
    assert ws[0]["runtime"] == {"interpreter": None, "venv_path": None}
    r = client.post("/workspaces/refresh")
    assert r.status_code == 200 and calls == [1]


def test_control_refresh_runs_host_cleanup_after_rescan(tmp_path):
    events = []

    async def cleanup():
        events.append("cleanup")

    app = create_control_app(
        started_at=STARTED,
        version="1",
        socket_path="/s",
        store=_registry_store(tmp_path),
        rescan=lambda: events.append("rescan"),
        after_rescan=cleanup,
    )

    response = TestClient(app).post("/workspaces/refresh")

    assert response.status_code == 200
    assert events == ["rescan", "cleanup"]


def test_no_store_means_no_registry_routes():
    client = _client()
    assert client.get("/health").json()["capabilities"]["registry"] is False
    assert client.get("/workspaces").status_code == 404
