"""Control app: the daemon's local identity surface. Version is captured at
process start (the upgrade-in-place lie #470 calls out), capabilities are the
#471/#472/#473 seams."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import mship
from mship.core.daemon.control import PROTOCOL, create_control_app, probe_control_socket

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
