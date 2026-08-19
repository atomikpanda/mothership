"""The daemon's registration client (#471 Task 6).

Everything the daemon does against the relay's host directory happens here, and
none of it may block, prompt, raise out of a tick, or write to disk on a
reconnect. The seams (`post`/`clock`/`rng`/`signer`/`issue_refresh`/
`reidentify`) are injected exactly like `tests/core/relay/test_health.py`, so
these tests use no sockets, no sleeps and no ssh-keygen.
"""

import base64
import hashlib
import json
import logging
import socket
from pathlib import Path

import pytest

from mship.core.daemon.capabilities import host_capability_payload, runner_block
from mship.core.daemon.identity import HostIdentity, force_reidentify
from mship.core.daemon import relay_link
from mship.core.daemon.relay_link import RelayLink
from mship.core.relay import host_contract
from mship.core.relay.config import RelayConfig
from mship.core.relay.enroll import fingerprint
from mship.core.relay.keys import relay_key_path

RELAY = RelayConfig(host="relay.example")
BASE = host_contract.enroll_base_url(RELAY.host)
PUBKEY = "ssh-ed25519 " + base64.b64encode(b"k" * 51).decode() + " mship-relay\n"


class _Resp:
    def __init__(
        self,
        status: int,
        body: dict | None = None,
        headers: dict | None = None,
        html: str | None = None,
    ):
        self.status_code = status
        self._body = {} if body is None else body
        self._html = html
        self.headers = headers or {}

    def json(self):
        if self._html is not None:  # what httpx does with a proxy error page
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class _Clock:
    """Fake wall clock: never sleeps, only advances when a test says so."""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _advance_past(clock: "_Clock", link) -> None:
    """Advance just past the link's scheduled delay (a float sum landing a hair
    under the boundary would silently skip an attempt and weaken the test)."""
    clock.advance(link.next_attempt_delay() + 0.001)


class _Relay:
    """Scriptable stand-in for the enroll app's `/hosts/*` + `/enroll` routes."""

    def __init__(
        self,
        register=(200, None),
        challenge=(200, None),
        enroll_ttl=host_contract.ENROLL_TTL_S,
        enroll=(200, None),
    ):
        self.register_status, self.register_detail = register
        self.challenge_status, self.challenge_detail = challenge
        self.enroll_ttl = enroll_ttl
        self.enroll_status, self.enroll_detail = enroll
        self.calls: list[tuple[str, str, dict | None]] = []
        self.nonce = "nonce-1"
        self.date_header: str | None = None
        self.transport_error: str | None = None
        self.challenge_html: str | None = None

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json))
        if self.transport_error:
            raise RuntimeError(self.transport_error)
        if url.endswith(host_contract.CHALLENGE_PATH):
            headers = {"Date": self.date_header} if self.date_header else {}
            if self.challenge_html is not None:
                return _Resp(200, headers=headers, html=self.challenge_html)
            body = (
                {"nonce": self.nonce, "expires_at": 0}
                if self.challenge_status == 200
                else {"detail": self.challenge_detail}
            )
            return _Resp(self.challenge_status, body, headers)
        if url.endswith("/enroll"):
            body = (
                {"id": "req-1", "status": "pending", "expires_in": self.enroll_ttl}
                if self.enroll_status == 200
                else {"detail": self.enroll_detail}
            )
            return _Resp(self.enroll_status, body)
        body = (
            {"status": "registered", "host_id": json["payload"]["host_id"]}
            if self.register_status == 200
            else {"detail": self.register_detail}
        )
        headers = {"Date": self.date_header} if self.date_header else {}
        return _Resp(self.register_status, body, headers)

    def posts_to(self, path: str) -> list[dict]:
        return [
            body
            for verb, url, body in self.calls
            if verb == "POST" and url.endswith(path)
        ]


def _seed_key(home: Path) -> None:
    """Write a relay keypair so nothing in these tests spawns ssh-keygen."""
    key = relay_key_path(home)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("PRIVATE")
    key.with_name(key.name + ".pub").write_text(PUBKEY)


def _sign(blob: bytes) -> str:
    return "SIG:" + hashlib.sha256(blob).hexdigest()


def _link(
    home: Path, relay: _Relay, clock: _Clock, *, rng=lambda: 0.5, **kw
) -> RelayLink:
    _seed_key(home)
    kw.setdefault("issue_refresh", lambda host_id: f"refresh-for-{host_id}")
    kw.setdefault("reidentify", lambda: HostIdentity(host_id="hst-new", created_at=""))
    return RelayLink(
        home,
        RELAY,
        post=relay.post,
        clock=clock,
        rng=rng,
        signer=_sign,
        **kw,
    )


# --- register_once: challenge → sign → register ----------------------------


def test_register_once_signs_the_posted_payload_and_returns_the_refresh(tmp_path: Path):
    relay = _Relay()
    link = _link(tmp_path, relay, _Clock())

    outcome = link.register_once()

    assert outcome.ok and outcome.kind == "registered"
    assert outcome.refresh == f"refresh-for-{link.host_id}"
    assert relay.calls[0] == (
        "POST",
        BASE + host_contract.CHALLENGE_PATH,
        {"key_fingerprint": link.key_fingerprint},
    )
    body = relay.posts_to(host_contract.REGISTER_PATH)[0]
    payload = body["payload"]
    assert payload["host_id"] == link.host_id
    assert payload["instance_id"] == link.instance_id
    assert payload["label"] == socket.gethostname()
    assert payload["key_fingerprint"] == fingerprint(PUBKEY)
    assert payload["subdomain"] == link.subdomain
    assert payload["public_url"] == f"https://{link.subdomain}.{RELAY.host}"
    assert payload["refresh"] == outcome.refresh
    assert payload["runner"] == {"enabled": False, "state": "disabled"}
    assert payload["capabilities"] == host_capability_payload()["capabilities"]
    # Signed over EXACTLY the bytes posted, with the challenge nonce inside.
    assert body["nonce"] == relay.nonce
    assert body["signature"] == _sign(host_contract.signing_blob(relay.nonce, payload))


def test_payload_survives_a_canonical_round_trip(tmp_path: Path):
    """What the relay re-serializes must be what we signed (contract bytes)."""
    relay = _Relay()
    _link(tmp_path, relay, _Clock()).register_once()
    payload = relay.posts_to(host_contract.REGISTER_PATH)[0]["payload"]
    assert host_contract.canonical_payload(
        json.loads(json.dumps(payload))
    ) == host_contract.canonical_payload(payload)


@pytest.mark.parametrize(
    "status, detail, kind",
    [
        (401, host_contract.UNAPPROVED_KEY_DETAIL, "unapproved"),
        (409, "another host holds this subdomain", "duplicate-identity"),
        (429, "too many outstanding challenges; try later", "refused"),
    ],
)
def test_typed_failures_never_raise(tmp_path: Path, status, detail, kind):
    relay = _Relay(register=(status, detail))
    outcome = _link(tmp_path, relay, _Clock()).register_once()
    assert outcome.ok is False
    assert outcome.kind == kind
    assert outcome.status_code == status
    assert detail in outcome.detail


def test_transport_failure_is_typed_not_raised(tmp_path: Path):
    relay = _Relay()
    relay.transport_error = "connect timeout"
    outcome = _link(tmp_path, relay, _Clock()).register_once()
    assert outcome.ok is False and outcome.kind == "transport"
    assert "connect timeout" in outcome.detail


def test_relay_date_header_samples_clock_skew(tmp_path: Path):
    """Task 9 reads this: the enroll server is the only different clock here."""
    relay = _Relay()
    clock = _Clock(t=1_755_003_600.0)  # 2025-08-12T13:00:00Z
    relay.date_header = "Tue, 12 Aug 2025 12:00:00 GMT"  # an hour behind us
    link = _link(tmp_path, relay, clock)
    assert link.clock_skew_seconds is None
    link.register_once()
    assert link.clock_skew_seconds == pytest.approx(3600, abs=1)


def test_register_post_date_alone_samples_skew(tmp_path: Path):
    """Both relay calls carry the enroll server's clock; a challenge without a
    `Date` must not leave the skew unknown — `mship daemon status` reports it
    from whichever answer carried one."""

    class _NoDateOnChallenge(_Relay):
        def post(self, url, json=None, **kw):
            if url.endswith(host_contract.CHALLENGE_PATH):
                self.calls.append(("POST", url, json))
                return _Resp(200, {"nonce": self.nonce, "expires_at": 0})
            return super().post(url, json=json, **kw)

    relay = _NoDateOnChallenge()
    clock = _Clock(t=1_755_003_600.0)  # 2025-08-12T13:00:00Z
    relay.date_header = "Tue, 12 Aug 2025 12:00:00 GMT"  # an hour behind us
    link = _link(tmp_path, relay, clock)

    link.register_once()

    assert link.clock_skew_seconds == pytest.approx(3600, abs=1)


def test_unparseable_date_header_leaves_skew_unknown(tmp_path: Path):
    relay = _Relay()
    relay.date_header = "not a date"
    link = _link(tmp_path, relay, _Clock())
    link.register_once()
    assert link.clock_skew_seconds is None


def test_a_200_that_is_not_json_is_typed_not_raised(tmp_path: Path):
    """A captive portal, a misrouted vhost and a proxy error page all answer 200
    with HTML; `register_once` must not raise on `.json()`."""
    relay = _Relay()
    relay.challenge_html = "<html>Sign in to continue</html>"
    outcome = _link(tmp_path, relay, _Clock()).register_once()
    assert outcome.ok is False and outcome.kind == "refused"
    assert "nonce" in outcome.detail
    assert relay.posts_to(host_contract.REGISTER_PATH) == []  # nothing signed blind


def test_a_stale_challenge_401_is_transient_not_unapproved(tmp_path: Path):
    """The relay 401s a lost nonce race the same way it 401s an unapproved key
    (CHALLENGE_TTL_S is 120s). An approved host on a slow link must not report
    awaiting-enrollment or re-post /enroll for a key already in `pubkeys/`."""
    for detail in host_contract.CHALLENGE_REFUSAL_DETAILS:
        relay = _Relay(register=(401, detail))
        link = _link(tmp_path / detail.replace(" ", "-"), relay, _Clock())
        link.tick()
        assert link.state != "awaiting-enrollment"
        assert relay.posts_to(host_contract.ENROLL_PATH) == []


def test_an_unrecognised_401_still_enrolls(tmp_path: Path):
    """The deliberate fallback: only the challenge refusals are transient, so a
    401 nobody recognises — a proxy's own body, an older relay's wording —
    enrolls. Enrolling once too often is recoverable; a host that never enrolls
    can never be approved."""
    relay = _Relay(register=(401, "Unauthorized"))
    link = _link(tmp_path, relay, _Clock())
    outcome = link.tick()
    assert outcome.kind == "unapproved"
    assert link.state == "awaiting-enrollment"
    assert len(relay.posts_to(host_contract.ENROLL_PATH)) == 1


# --- tick(): jittered, capped, ceiling-free backoff (AC2/AC3) --------------


def _fail(relay: _Relay, why: str = "boom") -> None:
    relay.transport_error = why


def test_tick_backs_off_and_a_success_resets_the_delay(tmp_path: Path):
    relay = _Relay()
    clock = _Clock()
    link = _link(tmp_path, relay, clock, rng=lambda: 0.0)  # no jitter subtracted

    _fail(relay)
    assert link.tick() is not None  # first tick is due
    first = link.next_attempt_delay()
    assert link.tick() is None  # not due yet
    clock.advance(first)
    assert link.tick() is not None
    assert link.next_attempt_delay() > first  # exponential growth

    relay.transport_error = None
    _advance_past(clock, link)
    assert link.tick().ok is True
    assert link.failure_count == 0
    # A healthy link heartbeats on the register interval, not the backoff.
    assert link.next_attempt_delay() == pytest.approx(host_contract.REGISTER_INTERVAL_S)


def test_backoff_has_no_ceiling_past_1024_failures(tmp_path: Path):
    """`float * 2**n` raises OverflowError at n == 1024 (assumption 5): the
    daemon is immortal, so that count is reachable — the delay must clamp."""
    relay = _Relay()
    clock = _Clock()
    link = _link(tmp_path, relay, clock, rng=lambda: 0.0)
    _fail(relay)
    for _ in range(1100):
        link.tick()
        _advance_past(clock, link)
    assert link.failure_count > 1024
    assert link.next_attempt_delay() == pytest.approx(host_contract.MAX_BACKOFF_S)


def test_jitter_never_schedules_past_the_cap(tmp_path: Path):
    """DIRECTORY_STALE_S is derived from MAX_BACKOFF_S, so a delay jittered
    ABOVE the cap would let a healthy reconnecting host read as stale."""
    for value in (0.0, 0.5, 1.0):
        relay = _Relay()
        clock = _Clock()
        link = _link(tmp_path / str(value), relay, clock, rng=lambda v=value: v)
        _fail(relay)
        for _ in range(12):
            link.tick()
            _advance_past(clock, link)
        assert 0 < link.next_attempt_delay() <= host_contract.MAX_BACKOFF_S
        assert link.next_attempt_delay() >= host_contract.MAX_BACKOFF_S * (
            1 - relay_link.JITTER
        )


def test_two_links_do_not_retry_on_the_same_tick(tmp_path: Path):
    """A fleet reconnecting after a relay redeploy must not stampede (AC3)."""
    unlucky, lucky = _Relay(), _Relay()
    clock = _Clock()
    slow = _link(tmp_path / "a", unlucky, clock, rng=lambda: 0.0)  # full delay
    fast = _link(tmp_path / "b", lucky, clock, rng=lambda: 1.0)  # 20% off it
    _fail(unlucky)
    _fail(lucky)
    slow.tick()
    fast.tick()
    assert fast.next_attempt_delay() < slow.next_attempt_delay()

    _advance_past(clock, fast)
    before = (len(unlucky.calls), len(lucky.calls))
    slow.tick()
    fast.tick()
    assert len(lucky.calls) > before[1] and len(unlucky.calls) == before[0]


# --- AC1/AC8: enrollment that stays alive, non-blocking, self-healing ------


def test_unapproved_key_posts_enroll_without_polling(tmp_path: Path):
    relay = _Relay(register=(401, host_contract.UNAPPROVED_KEY_DETAIL))
    link = _link(tmp_path, relay, _Clock())

    link.tick()

    assert link.state == "awaiting-enrollment"
    assert link.should_dial() is True  # ssh may still try; the log classifies
    assert relay.posts_to("/enroll") == [
        {"pubkey": PUBKEY.strip(), "hostname": socket.gethostname()}
    ]
    # Never the 1800s wait loop: nothing polls /status/{rid}.
    assert not any("/status/" in url for _verb, url, _body in relay.calls)


def test_enroll_is_reposted_on_a_schedule_shorter_than_the_store_ttl(tmp_path: Path):
    """A VM provisioned at 02:00 must still be approvable at 09:00 (AC1)."""
    assert host_contract.ENROLL_REPOST_INTERVAL_S < host_contract.ENROLL_TTL_S
    relay = _Relay(register=(401, host_contract.UNAPPROVED_KEY_DETAIL))
    clock = _Clock()
    link = _link(tmp_path, relay, clock)

    link.tick()
    assert len(relay.posts_to("/enroll")) == 1
    # Ticking through the backoff does NOT re-post: one pending record, not one
    # per tick (the relay's 50-slot pending cap would 429 the whole fleet).
    for _ in range(6):
        _advance_past(clock, link)
        link.tick()
    assert len(relay.posts_to("/enroll")) == 1

    clock.advance(host_contract.ENROLL_REPOST_INTERVAL_S)
    link.tick()
    assert len(relay.posts_to("/enroll")) == 2

    # Seven hours of ticking keeps the request alive across the store's TTL.
    elapsed = 0.0
    while elapsed < 7 * 3600:
        clock.advance(60.0)
        elapsed += 60.0
        link.tick()
    assert (
        len(relay.posts_to("/enroll"))
        >= 7 * 3600 // host_contract.ENROLL_REPOST_INTERVAL_S - 1
    )
    assert link.state == "awaiting-enrollment"


def test_enroll_repost_follows_the_servers_advertised_ttl(tmp_path: Path):
    relay = _Relay(
        register=(401, host_contract.UNAPPROVED_KEY_DETAIL),
        enroll_ttl=60,
    )
    clock = _Clock()
    link = _link(tmp_path, relay, clock)

    started_at = clock()
    link.tick()
    while len(relay.posts_to("/enroll")) == 1 and clock() - started_at < 60:
        _advance_past(clock, link)
        link.tick()

    assert len(relay.posts_to("/enroll")) == 2
    assert clock() - started_at < 60


@pytest.mark.parametrize("status", [429, 500])
def test_failed_enroll_response_retries_on_the_next_registration_attempt(
    tmp_path: Path, status: int
):
    relay = _Relay(
        register=(401, host_contract.UNAPPROVED_KEY_DETAIL),
        enroll=(status, "enrollment unavailable"),
    )
    clock = _Clock()
    link = _link(tmp_path, relay, clock)

    link.tick()
    assert len(relay.posts_to("/enroll")) == 1
    assert "enrollment unavailable" in (link.last_error or "")

    _advance_past(clock, link)
    link.tick()

    assert len(relay.posts_to("/enroll")) == 2


def test_challenge_stage_unapproved_self_heals_with_no_prompt(tmp_path: Path):
    relay = _Relay(challenge=(401, host_contract.UNAPPROVED_KEY_DETAIL))
    clock = _Clock()
    link = _link(tmp_path, relay, clock)

    link.tick()

    assert link.state == "awaiting-enrollment"
    assert len(relay.posts_to(host_contract.ENROLL_PATH)) == 1

    relay.challenge_status, relay.challenge_detail = 200, None  # owner approved
    _advance_past(clock, link)
    outcome = link.tick()

    assert outcome.ok and link.state == "registered"
    assert link.refresh == f"refresh-for-{link.host_id}"


# --- AC4b: a 409 is self-healing, and never needs a terminal ---------------


def test_duplicate_identity_stops_dialling_and_reports_the_relay_detail(tmp_path: Path):
    relay = _Relay(register=(409, "duplicate identity; run mship daemon reidentify"))
    link = _link(tmp_path, relay, _Clock())
    link.tick()
    assert link.state == "duplicate-identity"
    assert "mship daemon reidentify" in (link.last_error or "")
    assert link.should_dial() is False


def test_consecutive_409s_auto_reidentify_loudly_and_return_to_enrollment(
    tmp_path: Path, caplog
):
    relay = _Relay(register=(409, "duplicate identity"))
    clock = _Clock()
    reidentified: list[int] = []

    def reidentify():
        reidentified.append(1)
        return HostIdentity(host_id="hst-fresh", created_at="")

    link = _link(tmp_path, relay, clock, reidentify=reidentify)
    original, original_subdomain = link.host_id, link.subdomain
    with caplog.at_level(logging.WARNING):
        for _ in range(RelayLink.DUPLICATE_REIDENTIFY_AFTER):
            link.tick()
            _advance_past(clock, link)

    assert reidentified == [1]
    assert link.host_id == "hst-fresh" != original
    assert link.subdomain != original_subdomain  # a new subdomain
    assert link.state == "awaiting-enrollment"
    assert link.should_dial() is True
    assert any("re-identif" in r.getMessage() for r in caplog.records)
    # The fresh key needs approving again, so the host reappears as pending.
    assert relay.posts_to("/enroll")


def test_a_success_clears_the_duplicate_counter(tmp_path: Path):
    relay = _Relay(register=(409, "duplicate identity"))
    clock = _Clock()
    calls: list[int] = []

    def reidentify():
        calls.append(1)
        return HostIdentity(host_id="hst-unexpected", created_at="")

    link = _link(tmp_path, relay, clock, reidentify=reidentify)
    for _ in range(RelayLink.DUPLICATE_REIDENTIFY_AFTER - 1):
        link.tick()
        _advance_past(clock, link)
    relay.register_status, relay.register_detail = 200, None
    link.tick()
    _advance_past(clock, link)
    relay.register_status, relay.register_detail = 409, "duplicate identity"
    link.tick()
    assert calls == []  # the streak restarted at the success


# --- AC11: a reconnect writes nothing ---------------------------------------


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(directory)): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def test_n_registrations_leave_the_daemon_state_dir_byte_identical(tmp_path: Path):
    from mship.core.daemon.host_auth import RefreshStore
    from mship.core.daemon.paths import daemon_state_dir

    relay = _Relay()
    clock = _Clock()
    store = RefreshStore(tmp_path, clock=clock)
    link = _link(
        tmp_path,
        relay,
        clock,
        issue_refresh=lambda host_id: store.issue_refresh(
            host_id=host_id, client="relay-directory"
        ),
    )
    assert link.register_once().ok  # first run mints state

    before = _snapshot(daemon_state_dir(tmp_path))
    assert "host-refresh.json" in before  # the file that could churn
    for _ in range(20):
        clock.advance(host_contract.REGISTER_INTERVAL_S * 2)
        assert link.tick().ok
    assert _snapshot(daemon_state_dir(tmp_path)) == before
    # Idempotent per host: the same credential is re-published, never re-minted.
    assert {
        b["payload"]["refresh"] for b in relay.posts_to(host_contract.REGISTER_PATH)
    } == {link.refresh}


# --- the capability seam + the default re-identify --------------------------


def test_the_default_refresh_store_runs_on_the_links_clock(tmp_path: Path):
    """Two clocks in one daemon is how a credential expires early (or never):
    the store the link builds for itself must date records by the same clock the
    link schedules on."""
    from mship.core.daemon.host_auth import REFRESH_TTL_S
    from mship.core.daemon.paths import host_refresh_path

    relay = _Relay()
    clock = _Clock(t=4_000_000_000.0)  # far from time.time()
    assert _link(tmp_path, relay, clock, issue_refresh=None).register_once().ok
    doc = json.loads(host_refresh_path(tmp_path).read_text())
    record = next(iter(doc["clients"].values()))
    assert record["created_at"] == clock.t
    assert record["expires_at"] == clock.t + REFRESH_TTL_S


def test_capability_payload_has_one_assembler():
    payload = host_capability_payload()
    assert payload["runner"] == runner_block(None)
    assert payload["capabilities"]["runner"] is False
    assert payload["capabilities"]["tunnel"] is True
    enabled = host_capability_payload({"enabled": True})
    assert enabled["runner"] == {"enabled": True, "state": "unknown"}
    assert enabled["capabilities"]["runner"] is True
    malformed = host_capability_payload({"enabled": "false"})
    assert malformed["runner"] == {"enabled": False, "state": "disabled"}
    assert malformed["capabilities"]["runner"] is False


def test_force_reidentify_mints_a_new_id_and_rotates_the_key(tmp_path: Path):
    from mship.core.daemon.identity import ensure_host_identity

    rotated: list[Path] = []
    first = ensure_host_identity(tmp_path, fingerprint="fp", rotate_key=lambda h: None)
    fresh = force_reidentify(tmp_path, rotate_key=rotated.append)

    assert fresh.host_id != first.host_id
    assert fresh.cloned_from == first.host_id
    assert fresh.reidentified is True
    assert rotated == [tmp_path]
    # Persisted, so a restart does not fall back to the shadowed identity.
    assert ensure_host_identity(tmp_path, fingerprint="fp").host_id == fresh.host_id
