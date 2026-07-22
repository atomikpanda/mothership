from pathlib import Path
from typer.testing import CliRunner
import typer

from mship.cli import relay as relay_cli
from mship.core.relay.enroll import RequestStore
from mship.core.relay.grants import GrantStore
from mship.core.relay.run_token import verify_run_token


def _app():
    app = typer.Typer()
    relay_cli.register(app, get_container=lambda: None)
    return app


def _approved_enrollment(tmp_path: Path) -> str:
    """Create + approve an enrollment so its id resolves as 'approved'."""
    store = RequestStore(tmp_path / "pending-store")
    # A shape-valid ed25519 pubkey (real header + base64-valid body). validate_pubkey
    # checks shape only — approval, not this string, is the security boundary.
    rid = store.create(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "A" * 43, "worker",
    )
    store.approve(rid, tmp_path / "pubkeys")
    return rid


def test_grant_sets_typed_grant_for_approved_enrollment(tmp_path: Path):
    rid = _approved_enrollment(tmp_path)
    result = CliRunner().invoke(_app(), [
        "relay", "grant", rid,
        "--provider", "github-app", "--repos", "acme/api,acme/web",
        "--store-dir", str(tmp_path / "pending-store"),
        "--grant-store-dir", str(tmp_path / "grants-store"),
    ])
    assert result.exit_code == 0, result.output
    grants = GrantStore(tmp_path / "grants-store").get_grants(rid)
    assert set(grants[0].scope.repos) == {"acme/api", "acme/web"}


def test_grant_rejects_unknown_enrollment(tmp_path: Path):
    result = CliRunner().invoke(_app(), [
        "relay", "grant", "deadbeef",
        "--provider", "github-app", "--repos", "acme/api",
        "--store-dir", str(tmp_path / "pending-store"),
        "--grant-store-dir", str(tmp_path / "grants-store"),
    ])
    assert result.exit_code != 0


def test_issue_run_token_within_ceiling_prints_token(tmp_path: Path):
    rid = _approved_enrollment(tmp_path)
    GrantStore(tmp_path / "grants-store").set_grant(
        rid, __import__("mship.core.relay.grants", fromlist=["Grant", "Scope"]).Grant(
            "github-app",
            __import__("mship.core.relay.grants", fromlist=["Scope"]).Scope(repos=("acme/api", "acme/web")),
        ),
    )
    result = CliRunner().invoke(_app(), [
        "relay", "issue-run-token", rid,
        "--repos", "acme/api", "--push-branch", "feat/x",
        "--grant-store-dir", str(tmp_path / "grants-store"),
        "--run-token-dir", str(tmp_path / "run-tokens-store"),
    ])
    assert result.exit_code == 0, result.output
    token = result.output.strip().split()[-1]              # last token printed
    rt = verify_run_token(tmp_path / "run-tokens-store", token)
    assert rt is not None and rt.enrollment_id == rid


def test_issue_run_token_repo_outside_ceiling_fails(tmp_path: Path):
    rid = _approved_enrollment(tmp_path)
    GrantStore(tmp_path / "grants-store").set_grant(
        rid, __import__("mship.core.relay.grants", fromlist=["Grant", "Scope"]).Grant(
            "github-app",
            __import__("mship.core.relay.grants", fromlist=["Scope"]).Scope(repos=("acme/api",)),
        ),
    )
    result = CliRunner().invoke(_app(), [
        "relay", "issue-run-token", rid,
        "--repos", "acme/secret", "--push-branch", "feat/x",
        "--grant-store-dir", str(tmp_path / "grants-store"),
        "--run-token-dir", str(tmp_path / "run-tokens-store"),
    ])
    assert result.exit_code != 0


def _grant(tmp_path: Path, rid: str, repos: tuple[str, ...]) -> None:
    from mship.core.relay.grants import Grant, Scope
    GrantStore(tmp_path / "grants-store").set_grant(rid, Grant("github-app", Scope(repos=repos)))


def _grant_cmd(tmp_path: Path, rid: str, repos: str):
    return CliRunner().invoke(_app(), [
        "relay", "grant", rid, "--provider", "github-app", "--repos", repos,
        "--store-dir", str(tmp_path / "pending-store"),
        "--grant-store-dir", str(tmp_path / "grants-store"),
    ])


def test_grant_rejects_malformed_repo(tmp_path: Path):
    # A slashless `--repos api` would be accepted then IndexError at mint time —
    # reject it up front with a clear error (Greptile).
    rid = _approved_enrollment(tmp_path)
    result = _grant_cmd(tmp_path, rid, "api")
    assert result.exit_code != 0
    assert not GrantStore(tmp_path / "grants-store").get_grants(rid)


def test_grant_rejects_multi_owner(tmp_path: Path):
    rid = _approved_enrollment(tmp_path)
    result = _grant_cmd(tmp_path, rid, "org-a/api,org-b/web")
    assert result.exit_code != 0
    assert not GrantStore(tmp_path / "grants-store").get_grants(rid)


def _issue_cmd(tmp_path: Path, rid: str, *, repos="acme/api", branch="feat/x", ttl="86400"):
    return CliRunner().invoke(_app(), [
        "relay", "issue-run-token", rid,
        "--repos", repos, "--push-branch", branch, "--ttl", ttl,
        "--grant-store-dir", str(tmp_path / "grants-store"),
        "--run-token-dir", str(tmp_path / "run-tokens-store"),
    ])


def test_issue_run_token_rejects_nonpositive_ttl(tmp_path: Path):
    # A non-positive TTL mints an immediately-expired (useless) token — reject it,
    # do not print a "success" secret (Greptile).
    rid = _approved_enrollment(tmp_path)
    _grant(tmp_path, rid, ("acme/api",))
    result = _issue_cmd(tmp_path, rid, ttl="0")
    assert result.exit_code != 0


def test_issue_run_token_rejects_empty_branch(tmp_path: Path):
    rid = _approved_enrollment(tmp_path)
    _grant(tmp_path, rid, ("acme/api",))
    result = _issue_cmd(tmp_path, rid, branch="   ")
    assert result.exit_code != 0
