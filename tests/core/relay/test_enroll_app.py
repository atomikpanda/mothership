from fastapi.testclient import TestClient
from mship.core.relay.enroll import RequestStore
from mship.core.relay.enroll_app import build_enroll_app

_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyBodyAAAAAAAAAAAAAAAAAAAAAAAA host"
RELAY = "mship-relay.atomikpanda.com"


def _client(tmp_path, cap=50):
    store = RequestStore(tmp_path / "s", max_pending=cap)
    return TestClient(build_enroll_app(store, relay_domain=RELAY)), store


def _ask_client(tmp_path):
    return TestClient(build_enroll_app(RequestStore(tmp_path / "store"), relay_domain=RELAY))


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


def test_enroll_rejects_oversized_pubkey(tmp_path):
    c, _ = _client(tmp_path)
    # A valid key-type prefix + an absurdly long body: the body bound must reject
    # this before we read+hash+store it. 4xx (pydantic 422 or our 400), never 2xx/5xx.
    oversized = "ssh-ed25519 " + "A" * 4096 + " host"
    r = c.post("/enroll", json={"pubkey": oversized, "hostname": "x"})
    assert 400 <= r.status_code < 500


def test_tls_check_allows_relay_owned_host(tmp_path):
    c = _ask_client(tmp_path)
    assert c.get("/tls-check", params={"domain": f"enroll.{RELAY}"}).status_code == 200
    assert c.get("/tls-check", params={"domain": f"w-92bbb7.{RELAY}"}).status_code == 200


def test_tls_check_rejects_foreign_host(tmp_path):
    c = _ask_client(tmp_path)
    assert c.get("/tls-check", params={"domain": "evil.com"}).status_code == 403


def test_tls_check_requires_domain(tmp_path):
    c = _ask_client(tmp_path)
    assert c.get("/tls-check").status_code in (400, 422)
