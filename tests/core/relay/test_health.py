from mship.core.relay.health import verify_relay_reachable

class _Resp:
    def __init__(self, status): self.status_code = status

def test_ok_when_authed_request_succeeds():
    calls = {}
    def get(url, headers=None, timeout=None, follow_redirects=None):
        calls["url"] = url; calls["auth"] = headers.get("Authorization")
        return _Resp(200)
    ok, detail = verify_relay_reachable("https://w-ab12.relay", "tok", get=get)
    assert ok is True
    assert calls["url"] == "https://w-ab12.relay/health"
    assert calls["auth"] == "Bearer tok"

def test_not_ok_on_401_explains_token():
    ok, detail = verify_relay_reachable("https://w.relay", "tok", get=lambda *a, **k: _Resp(401))
    assert ok is False and "token" in detail.lower()

def test_not_ok_on_exception_carries_reason():
    def boom(*a, **k): raise RuntimeError("name resolution failed")
    ok, detail = verify_relay_reachable("https://w.relay", "tok", get=boom)
    assert ok is False and "name resolution failed" in detail
