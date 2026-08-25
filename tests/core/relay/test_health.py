from mship.core.relay.health import (
    probe_health,
    verify_relay_reachable,
    wait_until_reachable,
)

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

def test_health_probe_does_not_follow_relay_redirects():
    seen = []

    def get(*_args, **kwargs):
        seen.append(kwargs["follow_redirects"])
        return _Resp(302)

    probe_health("https://w-ab12.relay", "", get=get)

    assert seen == [False]

def test_health_probe_omits_authorization_when_token_is_empty():
    seen = {}

    def get(*_args, **kwargs):
        seen["headers"] = kwargs["headers"]
        return _Resp(200)

    probe = probe_health("https://w-ab12.relay", "", get=get)

    assert probe.ok is True
    assert not seen["headers"]


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


# --- probe_health: the one prober, with the status code exposed ---

def test_probe_health_reports_status_code():
    p = probe_health("https://w-ab12.relay", "tok", get=lambda *a, **k: _Resp(503))
    assert p.ok is False
    assert p.status_code == 503
    assert p.error is None

def test_probe_health_ok_on_2xx():
    p = probe_health("https://w-ab12.relay", "tok", get=lambda *a, **k: _Resp(204))
    assert p.ok is True and p.status_code == 204

def test_probe_health_carries_transport_error_and_never_raises():
    def boom(*a, **k): raise RuntimeError("name resolution failed")
    p = probe_health("https://w-ab12.relay", "tok", get=boom)
    assert p.ok is False and p.status_code is None
    assert "name resolution failed" in p.error


# --- the body + Date, for the tunnel's read-back (#471 AC4b) ---------------
#
# `probe_health` is the ONE prober, so the daemon's "who is actually answering
# on my subdomain?" check reads the parsed `/health` body from here rather than
# growing a second prober beside it.

class _JsonResp:
    def __init__(self, status, body=None, headers=None, raises=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._body


def test_probe_health_carries_the_parsed_body_and_date():
    p = probe_health("https://w.relay", "tok", get=lambda *a, **k: _JsonResp(
        200, {"status": "ok", "instance_id": "inst-1"}, {"Date": "Mon, 18 Aug 2026 12:00:00 GMT"}))
    assert p.body == {"status": "ok", "instance_id": "inst-1"}
    assert p.date_header == "Mon, 18 Aug 2026 12:00:00 GMT"


def test_probe_health_body_is_none_on_malformed_json_and_never_raises():
    # a captive portal / proxy error page answers 200 with HTML
    p = probe_health("https://w.relay", "tok", get=lambda *a, **k: _JsonResp(
        200, raises=ValueError("Expecting value: line 1 column 1 (char 0)")))
    assert p.ok is True and p.body is None and p.date_header is None


def test_probe_health_body_is_none_for_a_non_object_json_body():
    p = probe_health("https://w.relay", "tok",
                     get=lambda *a, **k: _JsonResp(200, ["not", "an", "object"]))
    assert p.body is None


def test_probe_health_body_and_date_default_to_none_on_transport_failure():
    def boom(*a, **k): raise RuntimeError("connection refused")
    p = probe_health("https://w.relay", "tok", get=boom)
    assert p.body is None and p.date_header is None


def test_probe_health_tolerates_a_response_without_json_or_headers():
    """`verify_relay_reachable` and `mship.core.topology` read only ok/status/
    error, and their callers hand in bare response stand-ins."""
    p = probe_health("https://w.relay", "tok", get=lambda *a, **k: _Resp(200))
    assert p.ok is True and p.body is None and p.date_header is None
