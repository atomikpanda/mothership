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


def test_a_ref_name_with_a_trailing_newline_is_out_of_scope():
    """The command git genuinely reads as a newline-suffixed ref: the NUL that
    starts the capabilities comes AFTER the newline, so the packet-level chomp
    never reaches it. Fed exactly this, real receive-pack answers
    `ng refs/mship/run/t1/api\\n funny refname` and creates nothing (verified) —
    but that is GIT's validation saving the run host. This check advertises
    itself as the one that decides, so it has to refuse the name itself."""
    command = f"{ZERO} {SHA} refs/mship/run/t1/api\n".encode() + b"\x00report-status"
    assert ref_commands(_body(command)) == ["refs/mship/run/t1/api\n"]
    with pytest.raises(RefScopeError):
        check_ref_scope(_body(command))


def test_an_object_id_with_a_trailing_newline_is_not_an_object_id():
    """Python's `$` also matches BEFORE a trailing newline, so an oid pattern
    anchored with `$` reads `<40 hex>\\n` as a whole, valid oid and hands the
    command on as in-scope. git's reader does not: it wants a space immediately
    after the 40th hex character and dies otherwise (`protocol error: expected
    old/new/ref, got ...` — verified). Refuse it where the parse IS the security
    boundary rather than count on git to."""
    command = f"{ZERO} {SHA}\n refs/mship/run/t1/api".encode()
    with pytest.raises(PktLineError):
        ref_commands(_body(command))


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


def test_a_zero_command_body_is_forwarded_rather_than_refused():
    """git's own probe, and therefore the feature's ordinary path.

    Before streaming a body larger than `http.postBuffer` (1 MiB by default),
    remote-curl POSTs a zero-command probe first: the body is exactly `0000`,
    `Content-Length: 4`. Refusing it as "contains no ref command" aborted every
    push over 1 MiB with `error: RPC failed; HTTP 400` and then a misleading
    `Everything up-to-date` (reproduced three ways) — and Task 8 pushes a
    synthesized working tree, routinely over 1 MiB.

    Safe to forward, and safe for the scope check to pass: a body with no
    commands names no refs, so there is nothing to check; real `git receive-pack
    --stateless-rpc <repo>` fed `0000` exits 0 with empty stdout (verified,
    git 2.43).
    """
    assert ref_commands(b"0000") == []
    check_ref_scope(b"0000")


@pytest.mark.parametrize("body", [b"", b"00", b"000"])
def test_a_body_that_is_not_a_pkt_line_stream_at_all_is_still_refused(body):
    """"No commands" and "unreadable" have to stay distinct.

    `0000` is a well-formed request that asks for nothing. A body that ends
    without either a flush or a command is not a receive-pack request at all:
    forwarded, it gets `fatal: the remote end hung up unexpectedly` out of git
    (verified) — a 500 for what is really a bad request.
    """
    with pytest.raises(PktLineError):
        ref_commands(body)


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


def test_a_zero_command_probe_is_answered_by_real_receive_pack(tmp_path):
    """The unit twin above says the probe is not refused; this says git is happy
    to be handed it — exit 0, empty stdout, no ref touched. Both halves matter:
    forwarding a body git chokes on would only move the failure from a 400 to a
    500."""
    repo = _repo(tmp_path / "api")
    before = _git("for-each-ref", "--format=%(refname)", cwd=repo)
    assert receive_pack(repo, b"0000") == b""
    assert _git("for-each-ref", "--format=%(refname)", cwd=repo) == before


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


# --- the HTTP routes ---------------------------------------------------------

from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.state import StateManager


def _app(tmp_path: Path, *, auth_token: str | None = None, with_config: bool = True):
    """Mirrors `tests/core/test_serve_exec.py::_app` (line 141) so the receive
    routes are exercised through exactly the app the exec routes are."""
    return create_app(
        specs_dir=tmp_path / "specs",
        state_manager=StateManager(tmp_path / ".mothership"),
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
        auth_token=auth_token,
        config=_config(tmp_path) if with_config else None,
    )


def test_receive_endpoints_require_the_bearer(tmp_path):
    """ac6: the run-host bearer gates the push, on both legs of it."""
    client = TestClient(_app(tmp_path, auth_token="secret"))
    assert client.get(
        "/git/api/info/refs", params={"service": "git-receive-pack"}
    ).status_code == 401
    assert client.post("/git/api/git-receive-pack", content=b"0000").status_code == 401


def test_advertisement_is_served_with_the_bearer(tmp_path):
    _repo(tmp_path / "api")
    client = TestClient(_app(tmp_path, auth_token="secret"))
    r = client.get(
        "/git/api/info/refs",
        params={"service": "git-receive-pack"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-git-receive-pack-advertisement"
    assert r.content.startswith(b"001f# service=git-receive-pack\n0000")


def test_upload_pack_service_is_not_served_here(tmp_path):
    """Narrow on purpose: this is a receive path, not a git host."""
    _repo(tmp_path / "api")
    client = TestClient(_app(tmp_path))
    r = client.get("/git/api/info/refs", params={"service": "git-upload-pack"})
    assert r.status_code == 403


def test_a_repo_the_workspace_does_not_declare_is_404(tmp_path):
    """The detail is asserted, not just the status: an unrouted path 404s too,
    so status alone would pass against an app with no receive routes at all."""
    client = TestClient(_app(tmp_path))
    for r in (
        client.get("/git/nope/info/refs", params={"service": "git-receive-pack"}),
        client.post("/git/nope/git-receive-pack", content=b"0000"),
    ):
        assert r.status_code == 404
        assert "unknown repo 'nope'" in r.json()["detail"]


def test_a_git_root_child_is_404_and_names_its_parent(tmp_path):
    """ac7 at the HTTP boundary."""
    r = TestClient(_app(tmp_path)).post("/git/server/git-receive-pack", content=b"0000")
    assert r.status_code == 404
    assert "api" in r.json()["detail"]


def test_a_push_outside_the_run_namespace_is_403_and_creates_nothing(tmp_path):
    """ac5 at the HTTP boundary.

    Carries a real (empty) packfile for the same reason the unit twin above
    does: without one, receive-pack refuses the push itself and the "no ref
    exists" assertion would hold even with the scope check moved AFTER git.
    With it, git would happily answer `ok refs/heads/attacker` — so the absent
    ref, and the 403 rather than a 500, are evidence the check ran first.
    """
    repo = _repo(tmp_path / "api")
    sha = _git("rev-parse", "HEAD", cwd=repo)
    body = (
        pkt_line(f"{'0' * 40} {sha} refs/heads/attacker\x00report-status".encode())
        + b"0000" + _empty_pack(repo)
    )
    r = TestClient(_app(tmp_path)).post("/git/api/git-receive-pack", content=body)
    assert r.status_code == 403
    assert "refs/mship/run" in r.json()["detail"]
    assert "attacker" not in _git("for-each-ref", "--format=%(refname)", cwd=repo)


def test_an_unparseable_body_is_400(tmp_path):
    _repo(tmp_path / "api")
    r = TestClient(_app(tmp_path)).post("/git/api/git-receive-pack", content=b"zzzz0000")
    assert r.status_code == 400


def test_a_compressed_body_is_refused_rather_than_mis_parsed(tmp_path):
    """git does not compress a receive-pack request body (verified against a
    real push), so a non-identity encoding means bytes whose refs cannot be
    checked. Refuse rather than hand them to receive-pack unexamined."""
    _repo(tmp_path / "api")
    r = TestClient(_app(tmp_path)).post(
        "/git/api/git-receive-pack", content=b"0000",
        headers={"Content-Encoding": "gzip"},
    )
    assert r.status_code == 400
    # The detail, not just the status: an unreadable body 400s from the pkt-line
    # parser anyway, so status alone would pass with no encoding check at all.
    assert "Content-Encoding" in r.json()["detail"]


def test_receive_is_unavailable_without_a_workspace_config(tmp_path):
    """Mirrors `POST /exec/{verb}` (serve.py:1411): 503 with an actionable
    message, never a bare 404."""
    client = TestClient(_app(tmp_path, with_config=False))
    assert client.post(
        "/git/api/git-receive-pack", content=b"0000"
    ).status_code == 503


# --- real git over a real socket ---------------------------------------------

import contextlib
import socket
import threading
import time

import uvicorn


@contextlib.contextmanager
def live_serve(app):
    """Run `app` under uvicorn on a loopback port for the duration of the block.

    Real git speaks real HTTP; TestClient does not. Anything asserting the smart-
    HTTP framing, the auth header git actually sends, or a push's effect on refs
    has to go through a socket.

    The listening socket is bound HERE and handed to uvicorn, rather than probing
    for a free port and letting uvicorn bind it: the port is never unbound
    between the two, so nothing else on the machine can take it in between. The
    block is only entered once `server.started` is set, which uvicorn does after
    `create_server` — so the port is accepting before the first `git push`, and
    the test cannot race the startup. `thread.is_alive()` is part of the wait
    condition so a server that dies during startup fails immediately with its
    reason rather than after the full timeout.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()       # uvicorn only closes it as part of its own shutdown
        raise RuntimeError(
            f"uvicorn did not start within 10s (thread alive: {thread.is_alive()})"
        )
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _push_env(token: str | None, config: dict[str, str] | None = None) -> dict[str, str]:
    """The env `core/run_transfer.extra_header_env` will build in Task 8: the
    bearer as an HTTP header through git's ENV config, and no interactive
    credential prompt (a rejected push must fail the command, not hang).
    `config` adds further git settings through the same mechanism.

    The index is taken from `_GIT_ENV` — the dict actually being handed to git —
    and NOT from `os.environ`. They differ: tests/conftest.py:41 sets
    GIT_CONFIG_COUNT=2 in `os.environ` from a session fixture, which runs AFTER
    this module is imported, so the `_GIT_ENV` snapshot above does not carry the
    count or its keys. Reading the count from `os.environ` (=2) while passing an
    env with no GIT_CONFIG_KEY_0 makes every push die with `error: missing
    config key GIT_CONFIG_KEY_0` / `fatal: unable to parse command-line config`
    — verified. Nothing is lost by starting at 0 here: those two keys only
    disable commit signing, and `_GIT_ENV` already points GIT_CONFIG_GLOBAL and
    GIT_CONFIG_SYSTEM at /dev/null, so no signing config reaches these repos at
    all.
    """
    env = {**_GIT_ENV, "GIT_TERMINAL_PROMPT": "0"}
    settings = dict(config or {})
    if token is not None:
        settings["http.extraHeader"] = f"Authorization: Bearer {token}"
    n = int(env.get("GIT_CONFIG_COUNT", "0"))
    for key, value in settings.items():
        env[f"GIT_CONFIG_KEY_{n}"] = key
        env[f"GIT_CONFIG_VALUE_{n}"] = value
        n += 1
    env["GIT_CONFIG_COUNT"] = str(n)
    return env


def _push(cwd: Path, url: str, refspec: str, token: str | None,
          config: dict[str, str] | None = None):
    return subprocess.run(
        ["git", "push", "--force", url, refspec],
        cwd=cwd, capture_output=True, text=True, env=_push_env(token, config),
    )


def _clone(host_repo: Path, dest: Path) -> Path:
    subprocess.run(
        ["git", "clone", "-q", str(host_repo), str(dest)],
        check=True, capture_output=True, env=_GIT_ENV,
    )
    return dest


def _refnames(repo: Path) -> str:
    return _git("for-each-ref", "--format=%(refname)", cwd=repo)


def test_a_real_git_push_lands_on_the_scratch_ref(tmp_path):
    """ac5/ac6/ac8/ac20 end to end: real git, real socket, real refs.

    `returncode == 0` is not the assertion that matters — a push can exit 0
    against the wrong repository. The ref on the HOST, at the sha the operator
    pushed, is: pointed at a second repo, `receive_repo_path` still answers and
    git still exits 0, and only the rev-parse below notices (verified).
    """
    host_repo = _repo(tmp_path / "api")
    operator = _clone(host_repo, tmp_path / "operator")
    (operator / "b.txt").write_text("two\n")
    _git("add", "-A", cwd=operator)
    _git("commit", "-qm", "second", cwd=operator)
    sha = _git("rev-parse", "HEAD", cwd=operator)

    with live_serve(_app(tmp_path, auth_token="tok-abc")) as base:
        result = _push(
            operator, f"{base}/git/api", f"{sha}:refs/mship/run/t1/api", "tok-abc"
        )
        assert result.returncode == 0, result.stderr
        assert _git("rev-parse", "refs/mship/run/t1/api", cwd=host_repo) == sha

        # ac8: the same ref force-updates on the next run.
        (operator / "b.txt").write_text("three\n")
        _git("commit", "-qam", "third", cwd=operator)
        sha2 = _git("rev-parse", "HEAD", cwd=operator)
        assert sha2 != sha
        assert _push(
            operator, f"{base}/git/api", f"{sha2}:refs/mship/run/t1/api", "tok-abc"
        ).returncode == 0

        # Deleting an absent ref is a no-op success (verified against real git:
        # `remote: warning: deleting a non-existent ref`, exit 0), so
        # `mship close` needs no "does it exist" probe.
        delete_absent = _push(
            operator, f"{base}/git/api", ":refs/mship/run/never/api", "tok-abc"
        )
        assert delete_absent.returncode == 0, delete_absent.stderr

    assert _git("rev-parse", "refs/mship/run/t1/api", cwd=host_repo) == sha2
    # The push never touched the host's checkout: `receive.denyCurrentBranch`
    # cannot apply outside refs/heads/.
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=host_repo) == "main"
    assert _git("status", "--porcelain", cwd=host_repo) == ""


def _commit_incompressible(operator: Path, size: int) -> str:
    """Commit `size` bytes of random data and return the sha.

    Random on purpose: git zlib-deflates a blob before it goes on the wire, so a
    file of zeros would be a few hundred bytes by the time `http.postBuffer` is
    consulted and would not exercise this path at all. The existing end-to-end
    push above misses the whole probe path for exactly that reason — its payload
    is a few hundred bytes.
    """
    (operator / "big.bin").write_bytes(os.urandom(size))
    _git("add", "-A", cwd=operator)
    _git("commit", "-qm", "big", cwd=operator)
    return _git("rev-parse", "HEAD", cwd=operator)


def test_a_push_over_the_post_buffer_takes_the_probe_path_and_lands(tmp_path):
    """The path EVERY push bigger than `http.postBuffer` takes.

    remote-curl will not buffer a request that large, so it cannot retry one; it
    POSTs a zero-command probe (`0000`, `Content-Length: 4`) first and only
    streams the real body once that is answered. Refusing the probe aborted the
    push with `error: RPC failed; HTTP 400` and then a misleading `Everything
    up-to-date`.

    `http.postBuffer=1024` forces the probe for a few hundred KB rather than
    several MB, so this stays cheap; the sibling below runs at the real default
    so the shipped configuration is exercised too.
    """
    host_repo = _repo(tmp_path / "api")
    operator = _clone(host_repo, tmp_path / "operator")
    sha = _commit_incompressible(operator, 200 * 1024)

    with live_serve(_app(tmp_path, auth_token="tok-abc")) as base:
        result = _push(
            operator, f"{base}/git/api", f"{sha}:refs/mship/run/t1/api", "tok-abc",
            config={"http.postBuffer": "1024"},
        )

    assert result.returncode == 0, result.stderr
    assert _git("rev-parse", "refs/mship/run/t1/api", cwd=host_repo) == sha


def test_a_multi_megabyte_push_lands_at_the_default_post_buffer(tmp_path):
    """The same path with nothing configured — 4 MiB against the 1 MiB default.

    Task 8 pushes a synthesized working tree, which is routinely this size, so
    the default is the configuration that actually ships. Kept alongside the
    cheap one above so a change to git's buffering default cannot quietly stop
    the probe from being covered.
    """
    host_repo = _repo(tmp_path / "api")
    operator = _clone(host_repo, tmp_path / "operator")
    sha = _commit_incompressible(operator, 4 * 1024 * 1024)

    with live_serve(_app(tmp_path, auth_token="tok-abc")) as base:
        result = _push(
            operator, f"{base}/git/api", f"{sha}:refs/mship/run/t1/api", "tok-abc"
        )

    assert result.returncode == 0, result.stderr
    assert _git("rev-parse", "refs/mship/run/t1/api", cwd=host_repo) == sha


def test_a_real_push_without_the_bearer_is_rejected(tmp_path):
    """ac6: an unauthenticated push fails — and fails rather than prompting.

    git turns the 401 into a credential request, which `GIT_TERMINAL_PROMPT=0`
    then refuses; the observed stderr is `fatal: could not read Username for
    'http://127.0.0.1:<port>': terminal prompts disabled` (exit 128), NOT the
    string "401". Assert the outcome — non-zero exit, no ref created, and an
    intelligible one-line reason rather than a hang or a traceback — not git's
    exact wording.
    """
    host_repo = _repo(tmp_path / "api")
    operator = _clone(host_repo, tmp_path / "operator")
    sha = _git("rev-parse", "HEAD", cwd=operator)
    before = _refnames(host_repo)

    with live_serve(_app(tmp_path, auth_token="tok-abc")) as base:
        result = _push(operator, f"{base}/git/api", f"{sha}:refs/mship/run/t1/api", None)

    assert result.returncode != 0
    assert "refs/mship/run/t1/api" not in _refnames(host_repo)
    assert _refnames(host_repo) == before
    # The failure is legible to whoever ran the push: one fatal line, no prompt
    # left hanging and no server traceback echoed back.
    assert "terminal prompts disabled" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_real_push_onto_a_branch_is_rejected(tmp_path):
    """ac5, with real git driving: the endpoint is not an arbitrary-ref writer.

    The ref assertion is the one that bites. Move `check_ref_scope` AFTER the
    `git receive-pack` call in `receive_pack` and this push still fails with
    HTTP 403 — the branch is simply created first (verified). Only the absent
    ref distinguishes "refused" from "done, then complained".
    """
    host_repo = _repo(tmp_path / "api")
    operator = _clone(host_repo, tmp_path / "operator")
    sha = _git("rev-parse", "HEAD", cwd=operator)

    with live_serve(_app(tmp_path, auth_token="tok-abc")) as base:
        result = _push(operator, f"{base}/git/api", f"{sha}:refs/heads/attacker", "tok-abc")

    assert result.returncode != 0
    assert "403" in result.stderr        # the endpoint's refusal, not a git error
    assert "refs/heads/attacker" not in _refnames(host_repo)
