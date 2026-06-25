from fastapi.testclient import TestClient
from mship.core.relay.enroll import RequestStore
from mship.core.relay.enroll_app import build_enroll_app

_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAAAAAA host"


def _client(tmp_path, cap=50):
    store = RequestStore(tmp_path / "s", max_pending=cap)
    return TestClient(build_enroll_app(store)), store


def test_enroll_creates_pending_and_status(tmp_path):
    c, _ = _client(tmp_path)
    r = c.post("/enroll", json={"pubkey": _PUB, "hostname": "laptop"})
    assert r.status_code == 200
    rid = r.json()["id"]
    assert c.get(f"/status/{rid}").json()["status"] == "pending"


def test_enroll_rejects_bad_key(tmp_path):
    c, _ = _client(tmp_path)
    assert c.post("/enroll", json={"pubkey": "garbage", "hostname": "x"}).status_code == 400


def test_enroll_over_cap_429(tmp_path):
    c, _ = _client(tmp_path, cap=1)
    c.post("/enroll", json={"pubkey": _PUB, "hostname": "a"})
    assert c.post("/enroll", json={"pubkey": _PUB, "hostname": "b"}).status_code == 429


def test_status_unknown(tmp_path):
    c, _ = _client(tmp_path)
    assert c.get("/status/deadbeef").json()["status"] == "unknown"
