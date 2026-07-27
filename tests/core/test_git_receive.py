"""The scoped git receive path.

Layered on purpose: pure pkt-line framing first (no git, no HTTP), then the
allowlist and receive-pack against REAL repositories, then the FastAPI routes,
then a real `git push` over a real socket. The framing is the security boundary,
so it is tested where nothing can hide behind a mock.
"""
import os
import subprocess
from pathlib import Path

import pytest

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.git_receive import (
    PktLineError,
    RefScopeError,
    UnknownReceiveRepoError,
    advertise_refs,
    check_ref_scope,
    pkt_line,
    receive_pack,
    receive_repo_path,
    ref_commands,
    service_advertisement,
)

ZERO = "0" * 40
SHA = "9647986511a9eb8e9260ca70fc90406674ece7a9"


def _body(*commands: bytes, pack: bytes = b"") -> bytes:
    return b"".join(pkt_line(c) for c in commands) + b"0000" + pack


def test_pkt_line_prefixes_a_four_hex_length_including_itself():
    assert pkt_line(b"abc") == b"0007abc"


def test_service_advertisement_matches_the_wire_prefix_git_expects():
    """Verified against a real `git push`: this exact prefix, then the
    `git receive-pack --http-backend-info-refs` output verbatim."""
    assert service_advertisement(b"REFS").startswith(b"001f# service=git-receive-pack\n0000")
    assert service_advertisement(b"REFS").endswith(b"REFS")


def test_first_command_capabilities_are_stripped():
    body = _body(f"{ZERO} {SHA} refs/mship/run/t1/api\x00report-status side-band-64k".encode())
    assert ref_commands(body) == ["refs/mship/run/t1/api"]


def test_every_command_is_reported_not_just_the_first():
    body = _body(
        f"{ZERO} {SHA} refs/mship/run/t1/api\x00report-status".encode(),
        f"{ZERO} {SHA} refs/heads/sneaky".encode(),
    )
    assert ref_commands(body) == ["refs/mship/run/t1/api", "refs/heads/sneaky"]


def test_a_delete_command_is_parsed():
    """Deleting a scratch ref is old=<sha>, new=<zeros> — the same ref-name
    control governs it."""
    body = _body(f"{SHA} {ZERO} refs/mship/run/t1/api".encode())
    assert ref_commands(body) == ["refs/mship/run/t1/api"]


def test_trailing_pack_data_is_ignored():
    body = _body(f"{ZERO} {SHA} refs/mship/run/t1/api".encode(), pack=b"PACK\x00\x01binary")
    assert ref_commands(body) == ["refs/mship/run/t1/api"]


@pytest.mark.parametrize("body", [
    b"zzzz0000",                    # length is not hex
    b"0003ab",                      # length below the 4-byte header
    b"00ff" + b"short",             # length runs past the body
    b"0010" + b"only two fields",   # not a ref command
])
def test_unparseable_bodies_raise_rather_than_being_guessed_at(body):
    """A body whose refs cannot be read is a body whose refs cannot be checked."""
    with pytest.raises(PktLineError):
        ref_commands(body)


def test_the_whole_remainder_of_a_command_is_the_ref_name():
    """git takes the ref name as everything after the two oids up to a NUL —
    spaces included: fed this command, real receive-pack answers
    `ng refs/heads/a b funny refname`. A parser that stopped at the next space
    would report the shorter, IN-SCOPE `refs/mship/run/t1/api` for a command git
    reads as a different ref. Read it git's way and the scope check refuses it."""
    command = f"{ZERO} {SHA} refs/mship/run/t1/api refs/heads/evil".encode()
    assert ref_commands(_body(command)) == ["refs/mship/run/t1/api refs/heads/evil"]
    with pytest.raises(RefScopeError):
        check_ref_scope(_body(command))


def test_commands_are_reported_when_the_body_ends_without_a_flush():
    """Only a flush ends the command list. Truncating the body instead makes
    real receive-pack die without touching a ref, so this is belt and braces —
    but the check must never see FEWER refs than git might act on, and a parser
    that treated end-of-body as end-of-list would silently drop the last one."""
    body = (
        pkt_line(f"{ZERO} {SHA} refs/mship/run/t1/api\x00report-status".encode())
        + pkt_line(f"{ZERO} {SHA} refs/heads/sneaky".encode())
    )
    assert ref_commands(body) == ["refs/mship/run/t1/api", "refs/heads/sneaky"]
    with pytest.raises(RefScopeError):
        check_ref_scope(body)


def test_check_ref_scope_accepts_the_run_namespace():
    check_ref_scope(_body(f"{ZERO} {SHA} refs/mship/run/t1/api".encode()))


def test_check_ref_scope_refuses_a_branch_write():
    """ac5. An unscoped receive path really does create refs/heads/<anything> —
    verified against a live prototype. This is the control that stops it."""
    with pytest.raises(RefScopeError) as exc:
        check_ref_scope(_body(f"{ZERO} {SHA} refs/heads/attacker".encode()))
    assert "refs/heads/attacker" in str(exc.value)


def test_one_out_of_scope_ref_refuses_the_whole_push():
    with pytest.raises(RefScopeError):
        check_ref_scope(_body(
            f"{ZERO} {SHA} refs/mship/run/t1/api".encode(),
            f"{ZERO} {SHA} refs/tags/v1".encode(),
        ))


def test_a_body_with_no_commands_is_refused():
    with pytest.raises(PktLineError):
        check_ref_scope(b"0000")


# --- real repositories -------------------------------------------------------

# The operator's own git config must not reach these repos: a global
# `commit.gpgsign`, `insteadOf`, or `hooksPath` would make the outcome depend on
# whose machine the suite runs on. Same reasoning (and same shape) as
# `_GIT_ENV` in tests/core/test_remote_preflight.py:581.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env=_GIT_ENV,
    ).stdout.strip()


def _repo(path: Path) -> Path:
    """A real, NON-BARE git repository with one commit — the shape a run host's
    checkout actually has."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", ".", cwd=path)
    (path / "a.txt").write_text("one\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    return path


def _empty_pack(repo: Path) -> bytes:
    """A valid packfile carrying zero objects.

    Not a detail: real receive-pack REFUSES a non-delete command that arrives
    without a pack ("unpack eof before pack header was fully read") and creates
    nothing, so a body that stops at the flush proves nothing about ordering.
    With this appended — and the pushed oid already in the repo, so no object is
    actually needed — git answers `unpack ok` / `ok refs/heads/attacker` and
    really does create the ref. That is what makes the "no ref exists" assertion
    below evidence that git was never run.
    """
    return subprocess.run(
        ["git", "pack-objects", "--stdout"], cwd=repo, input=b"",
        capture_output=True, check=True, env=_GIT_ENV,
    ).stdout


def _config(tmp_path: Path) -> WorkspaceConfig:
    """One top-level repo plus a `git_root` child of it. Top-level paths are
    absolute because `ConfigLoader.load` resolves them that way in production
    (config.py:577) and `receive_repo_path` hands the value straight to git."""
    return WorkspaceConfig(
        workspace="t",
        repos={
            "api": RepoConfig(path=tmp_path / "api", type="service"),
            "server": RepoConfig(path=Path("server"), type="service", git_root="api"),
        },
    )


def test_allowlist_resolves_a_declared_repo(tmp_path):
    assert receive_repo_path(_config(tmp_path), "api") == tmp_path / "api"


def test_allowlist_refuses_a_repo_this_workspace_does_not_declare(tmp_path):
    """ac5: not a general-purpose git host — an undeclared repo is refused."""
    with pytest.raises(UnknownReceiveRepoError) as exc:
        receive_repo_path(_config(tmp_path), "somebody-elses-repo")
    assert "api" in str(exc.value)          # names what IS known


def test_allowlist_refuses_a_git_root_child_and_names_its_parent(tmp_path):
    """ac7: a child has no git directory of its own, so the push belongs to the
    parent — and the refusal has to say which parent."""
    with pytest.raises(UnknownReceiveRepoError) as exc:
        receive_repo_path(_config(tmp_path), "server")
    assert "api" in str(exc.value)


def test_advertisement_is_the_prefix_plus_real_receive_pack_output(tmp_path):
    repo = _repo(tmp_path / "api")
    head = _git("rev-parse", "HEAD", cwd=repo)
    body = advertise_refs(repo)
    assert body.startswith(b"001f# service=git-receive-pack\n0000")
    assert head.encode() in body


def test_receive_pack_refuses_an_out_of_scope_push_without_touching_the_repo(tmp_path):
    """The decisive assertion: no ref is created, because git was never run.

    The body is one real receive-pack would ACCEPT (verified: fed to
    `git receive-pack --stateless-rpc` it answers `ok refs/heads/attacker` and
    the branch appears), so the absent ref can only mean the scope check ran
    first.
    """
    repo = _repo(tmp_path / "api")
    zero, sha = "0" * 40, _git("rev-parse", "HEAD", cwd=repo)
    body = (
        pkt_line(f"{zero} {sha} refs/heads/attacker\x00report-status".encode())
        + b"0000" + _empty_pack(repo)
    )

    with pytest.raises(RefScopeError):
        receive_pack(repo, body)

    refs = _git("for-each-ref", "--format=%(refname)", cwd=repo)
    assert "refs/heads/attacker" not in refs


# --- pushes from a shallow clone ---------------------------------------------
#
# Reachable, not theoretical: `git worktree add` from a `--depth 1` clone
# succeeds and the worktree is shallow too (`rev-parse
# --is-shallow-repository` -> true), so an operator who shallow-cloned a
# workspace repo gets shallow task worktrees — and every push from one leads
# with `shallow <oid>` lines. Left unparsed those refuse the whole push with
# "malformed ref command", which is safe but wrong.


def test_a_shallow_line_is_not_a_ref_command(tmp_path):
    """The real thing, from a real shallow worktree: capture the bytes git
    actually sends by pushing through a `git receive-pack` that tees its stdin
    (the command section is identical over HTTP — verified against a live
    `mship serve`-shaped endpoint)."""
    origin = _repo(tmp_path / "origin")
    (origin / "a.txt").write_text("two\n")
    _git("commit", "-qam", "second", cwd=origin)
    # The run host holds a full clone. It has to: git refuses a push whose
    # history is truncated ("shallow update not allowed") unless the receiving
    # end already has the missing objects, so a shallow client only works
    # against a host that does — which is exactly the run-host case.
    host = tmp_path / "host"
    _git("clone", "-q", str(origin), str(host), cwd=tmp_path)
    _git("clone", "-q", "--depth", "1", f"file://{origin}", str(tmp_path / "shallow"),
         cwd=tmp_path)
    shallow = tmp_path / "shallow"
    _git("worktree", "add", "-q", str(tmp_path / "wt"), "-b", "wt", cwd=shallow)
    worktree = tmp_path / "wt"
    assert _git("rev-parse", "--is-shallow-repository", cwd=worktree) == "true"

    captured = tmp_path / "body.bin"
    tee = tmp_path / "tee-receive-pack"
    tee.write_text(f'#!/bin/sh\ntee "{captured}" | git receive-pack "$@"\n')
    tee.chmod(0o755)
    _git("push", f"--receive-pack={tee}", str(host),
         "HEAD:refs/mship/run/t1/api", cwd=worktree)

    body = captured.read_bytes()
    assert b"shallow " in body[:64]
    assert ref_commands(body) == ["refs/mship/run/t1/api"]
    check_ref_scope(body)


def test_a_shallow_push_still_has_its_refs_checked():
    """Skipping the line must not skip the control it precedes."""
    body = _body(
        f"shallow {SHA}".encode(),
        f"{ZERO} {SHA} refs/heads/attacker".encode(),
    )
    with pytest.raises(RefScopeError):
        check_ref_scope(body)


@pytest.mark.parametrize("line", [
    b"shallow refs/heads/evil",              # not an oid where git demands one
    b"shallow " + SHA.encode() + b" more",   # trailing junk after the oid
    b"shallowness is a virtue",              # only the exact prefix is a shallow line
])
def test_a_line_git_would_not_read_as_shallow_is_still_refused(line):
    """git's own reader dies on `shallow <not-an-oid>` rather than skipping it;
    a laxer skip here would be a way to hide a command from the scope check."""
    with pytest.raises(PktLineError):
        ref_commands(_body(line))
