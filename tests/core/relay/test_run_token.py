from pathlib import Path
from mship.core.relay.grants import Scope
from mship.core.relay.run_token import issue_run_token, verify_run_token
from mship.core.relay.token_clock import SKEW_SECONDS, is_expired


def test_issue_returns_plaintext_and_verify_accepts(tmp_path: Path):
    clock = lambda: 1000.0
    token = issue_run_token(
        tmp_path, enrollment_id="enr1",
        scope=Scope(repos=("acme/api", "acme/web"), push_branch="feat/x"),
        ttl_seconds=3600, clock=clock,
    )
    assert "." in token
    rt = verify_run_token(tmp_path, token, clock=clock)
    assert rt is not None
    assert rt.enrollment_id == "enr1"
    assert set(rt.scope.repos) == {"acme/api", "acme/web"}
    assert rt.scope.push_branch == "feat/x"


def test_only_hash_persisted_not_plaintext(tmp_path: Path):
    token = issue_run_token(tmp_path, enrollment_id="enr1",
                            scope=Scope(repos=("acme/api",), push_branch="feat/x"),
                            ttl_seconds=3600)
    _id, secret = token.split(".", 1)
    for f in (tmp_path / "run-tokens").glob("*.json"):
        assert secret not in f.read_text()


def test_verify_rejects_tampered_secret(tmp_path: Path):
    token = issue_run_token(tmp_path, enrollment_id="enr1",
                            scope=Scope(repos=("acme/api",), push_branch="feat/x"),
                            ttl_seconds=3600)
    token_id, _secret = token.split(".", 1)
    assert verify_run_token(tmp_path, f"{token_id}.wrong") is None


def test_verify_rejects_expired(tmp_path: Path):
    token = issue_run_token(tmp_path, enrollment_id="enr1",
                            scope=Scope(repos=("acme/api",), push_branch="feat/x"),
                            ttl_seconds=100, clock=lambda: 1000.0)
    assert verify_run_token(tmp_path, token, clock=lambda: 2000.0) is None


def test_verify_rejects_unknown_token(tmp_path: Path):
    (tmp_path / "run-tokens").mkdir(parents=True, exist_ok=True)
    assert verify_run_token(tmp_path, "deadbeef.secret") is None


# --- expiry is owned by token_clock, not by a bare `clock() >= expires_at` ---
# The run token is presented across machines (worker → relay), so the relay's
# wall clock can legitimately sit a little ahead of the issuer's; the shared
# helper's skew grace is what keeps that from 401ing a live run.


def test_verify_accepts_within_the_skew_grace(tmp_path: Path):
    token = issue_run_token(tmp_path, enrollment_id="enr1",
                            scope=Scope(repos=("acme/api",), push_branch="feat/x"),
                            ttl_seconds=100, clock=lambda: 1000.0)
    just_inside = 1100.0 + SKEW_SECONDS - 1
    assert verify_run_token(tmp_path, token, clock=lambda: just_inside) is not None


def test_verify_rejects_beyond_the_skew_grace(tmp_path: Path):
    token = issue_run_token(tmp_path, enrollment_id="enr1",
                            scope=Scope(repos=("acme/api",), push_branch="feat/x"),
                            ttl_seconds=100, clock=lambda: 1000.0)
    just_outside = 1100.0 + SKEW_SECONDS
    assert verify_run_token(tmp_path, token, clock=lambda: just_outside) is None


def test_non_finite_expiry_fails_closed():
    for expires_at in (float("nan"), float("inf"), float("-inf")):
        assert is_expired(expires_at, now=1000.0) is True
