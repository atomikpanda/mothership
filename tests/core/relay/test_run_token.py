from pathlib import Path
from mship.core.relay.grants import Scope
from mship.core.relay.run_token import issue_run_token, verify_run_token


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
