from mship.core.relay.health import verify_relay_reachable, wait_until_reachable

class _Resp:
    def __init__(self, status): self.status_code = status

class _Clock:
    """Fake monotonic clock: sleep() advances the clock instead of blocking."""
    def __init__(self): self.t = 0.0
    def now(self): return self.t
    def sleep(self, dt): self.t += dt

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


# --- wait_until_reachable: retry the public probe through startup latency ---

def test_wait_retries_until_reachable():
    # tunnel route not registered yet (404), then comes up (200) → succeed
    seq = [_Resp(404), _Resp(200)]
    calls = []
    def get(*a, **k):
        calls.append(1); return seq[len(calls) - 1]
    clk = _Clock()
    ok, detail = wait_until_reachable("https://w.relay", "tok", get=get,
                                      timeout=30, interval=3,
                                      clock=clk.now, sleep=clk.sleep)
    assert ok is True and detail == "ok"
    assert len(calls) == 2          # retried once before it came up

def test_wait_retries_transport_errors():
    # connection refused while the ssh tunnel is still being established
    calls = []
    def get(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("connection refused")
        return _Resp(200)
    clk = _Clock()
    ok, detail = wait_until_reachable("https://w.relay", "tok", get=get,
                                      timeout=30, interval=3,
                                      clock=clk.now, sleep=clk.sleep)
    assert ok is True
    assert len(calls) == 3

def test_wait_times_out_returns_last_detail():
    clk = _Clock()
    ok, detail = wait_until_reachable("https://w.relay", "tok",
                                      get=lambda *a, **k: _Resp(404),
                                      timeout=10, interval=3,
                                      clock=clk.now, sleep=clk.sleep)
    assert ok is False and "404" in detail
    assert clk.t >= 10              # actually waited out the deadline

def test_wait_does_not_retry_on_auth_failure():
    # a stale token never recovers — fail fast, don't burn the whole window
    calls = []
    def get(*a, **k):
        calls.append(1); return _Resp(401)
    clk = _Clock()
    ok, detail = wait_until_reachable("https://w.relay", "tok", get=get,
                                      timeout=30, interval=3,
                                      clock=clk.now, sleep=clk.sleep)
    assert ok is False and "token" in detail.lower()
    assert len(calls) == 1          # no retry
    assert clk.t == 0               # never slept
