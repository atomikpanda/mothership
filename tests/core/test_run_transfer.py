"""Commit synthesis: a real commit object whose tree is the working tree, built
without touching the operator's repository.

Real git, real repositories, real ShellRunner throughout. The spec's own risk
list says the failure here is SILENT — an empty temporary index drops
tracked-and-gitignored files with no error — so nothing in this file is allowed
to be asserted against a mock.
"""
import os
import subprocess
from pathlib import Path

import pytest

from mship.core.run_transfer import RunTransferError, synthesize_commit
from mship.util.shell import ShellRunner


def _git_env() -> dict[str, str]:
    """Keep the operator's global git config out of these repos, exactly as
    tests/core/test_git_receive.py:195 does and for the same reason.

    Built per call, not snapshotted at import: `tests/conftest.py` installs its
    GIT_CONFIG_* signing overrides in a session fixture that runs AFTER this
    module is imported, and two tests below monkeypatch os.environ. A snapshot
    would silently hand git a different environment than the one under test.
    The identity is pinned unconditionally so the helper keeps working in the
    test that strips identity from os.environ.
    """
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env=_git_env(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo on a feature branch with one commit, a .gitignore, and a
    deliberately ignored file."""
    path = tmp_path / "api"
    path.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=path)
    (path / "a.txt").write_text("one\n")
    (path / ".gitignore").write_text("ignored.txt\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    _git("checkout", "-q", "-b", "feat/x", cwd=path)
    return path


def _head(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo)


def _dirty(repo: Path) -> None:
    (repo / "a.txt").write_text("one\nedited\n")
    (repo / "untracked.txt").write_text("scratch\n")
    (repo / "ignored.txt").write_text("secret\n")


def _tree_files(repo: Path, sha: str) -> list[str]:
    return _git("ls-tree", "-r", "--name-only", sha, cwd=repo).splitlines()


def test_the_synthesized_tree_is_the_working_tree(repo):
    """ac1: what the run host is asked to materialize contains the modified file
    exactly as it is on disk."""
    _dirty(repo)
    sha = synthesize_commit(ShellRunner(), repo, base_sha=_head(repo))

    blob = _git("show", f"{sha}:a.txt", cwd=repo)
    assert blob == "one\nedited"
    assert (repo / "a.txt").read_text() == blob + "\n"


def test_untracked_files_travel_and_gitignored_ones_do_not(repo):
    """ac11, pinned in both directions."""
    _dirty(repo)
    files = _tree_files(repo, synthesize_commit(ShellRunner(), repo, base_sha=_head(repo)))
    assert "untracked.txt" in files
    assert "ignored.txt" not in files


def test_a_tracked_file_that_is_also_gitignored_survives(repo):
    """ac1's second half, and the spec's top risk. Verified failure mode: with
    an EMPTY temporary index `git add -A` silently skips this file, because git
    will not add an ignored path that is not already in the index — the run host
    would then execute a tree missing a file the operator can plainly see.
    Seeding the scratch index from the base commit is what keeps it."""
    (repo / ".gitignore").write_text("ignored.txt\na.txt\n")
    files = _tree_files(repo, synthesize_commit(ShellRunner(), repo, base_sha=_head(repo)))
    assert "a.txt" in files


def test_a_modified_tracked_and_gitignored_file_carries_its_WORKING_content(repo):
    """Seeding from the base commit must not mean shipping the base commit's
    version: it seeds, then `git add -A` overwrites from the working tree."""
    (repo / ".gitignore").write_text("a.txt\n")
    (repo / "a.txt").write_text("edited while ignored\n")
    sha = synthesize_commit(ShellRunner(), repo, base_sha=_head(repo))
    assert _git("show", f"{sha}:a.txt", cwd=repo) == "edited while ignored"


def test_a_deleted_tracked_file_is_absent_from_the_tree(repo):
    (repo / "a.txt").unlink()
    sha = synthesize_commit(ShellRunner(), repo, base_sha=_head(repo))
    assert "a.txt" not in _tree_files(repo, sha)


def test_local_state_is_identical_before_and_after(repo):
    """ac2, the destructive-surprise guard: `git add -A` against the DEFAULT
    index would stage the operator's work in progress."""
    _dirty(repo)

    def snapshot():
        return {
            "head": _git("rev-parse", "HEAD", cwd=repo),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo),
            "status": _git("status", "--porcelain", cwd=repo),
            "index": _git("diff", "--cached", "--name-only", cwd=repo),
            "branches": _git("branch", "--list", cwd=repo),
        }

    before = snapshot()
    sha = synthesize_commit(ShellRunner(), repo, base_sha=before["head"])
    after = snapshot()

    assert after == before
    assert before["index"] == ""                     # nothing was staged
    assert sha != before["head"]


def test_the_commit_is_on_no_branch(repo):
    """ac2/ac14: it is not history anyone can build on."""
    _dirty(repo)
    base = _head(repo)
    sha = synthesize_commit(ShellRunner(), repo, base_sha=base)
    assert _git("branch", "--contains", sha, "--all", cwd=repo) == ""
    assert _git("rev-parse", f"{sha}^", cwd=repo) == base


def test_the_parent_is_the_sha_it_was_given_not_a_re_resolved_head(repo):
    """Fix 9 carried onto this path. `inspect` certifies a sha; something else
    committing in this worktree in the meantime (a subagent, a background job —
    this workspace's normal pattern) must not silently re-root the snapshot."""
    _dirty(repo)
    certified = _head(repo)

    (repo / "raced.txt").write_text("raced\n")
    _git("add", "raced.txt", cwd=repo)
    _git("commit", "-qm", "raced after inspection", cwd=repo)
    assert _head(repo) != certified

    sha = synthesize_commit(ShellRunner(), repo, base_sha=certified)
    assert _git("rev-parse", f"{sha}^", cwd=repo) == certified


def test_it_works_in_a_repo_with_no_configured_identity(tmp_path, monkeypatch):
    """`git commit-tree` needs an author; taking it from git config would make
    synthesis fail on a machine that has none. It is pinned to mship instead."""
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)

    path = tmp_path / "no-identity"
    path.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=path)
    (path / "a.txt").write_text("one\n")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    (path / "a.txt").write_text("edited\n")

    # The identity can only have come from synthesize_commit: nothing in this
    # repo's config supplies one.
    assert subprocess.run(
        ["git", "config", "user.email"], cwd=path, capture_output=True,
        text=True, env={k: v for k, v in _git_env().items()
                        if not k.startswith("GIT_AUTHOR")
                        and not k.startswith("GIT_COMMITTER")},
    ).returncode != 0

    sha = synthesize_commit(ShellRunner(), path, base_sha=_head(path))
    assert "mship" in _git("show", "-s", "--format=%an <%ae>", sha, cwd=path)


def test_commit_signing_cannot_block_synthesis(repo, monkeypatch):
    """`git commit-tree` does not honour `commit.gpgsign` (verified with
    `-c commit.gpgsign=true -c gpg.program=/bin/false`), so an operator with
    signing configured cannot be left waiting on a passphrase prompt that a
    captured-output subprocess never shows anyone."""
    # tests/conftest.py's session fixture force-disables commit.gpgsign for the
    # whole suite via GIT_CONFIG_* env vars, which outrank repo config. Left in
    # place, this test would assert nothing at all.
    for var in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"):
        monkeypatch.delenv(var, raising=False)

    _git("config", "commit.gpgsign", "true", cwd=repo)
    _git("config", "gpg.program", "/bin/false", cwd=repo)
    _dirty(repo)

    # Control: the signing config really is live on this repo — `git commit`
    # under it cannot produce a commit object.
    control = subprocess.run(
        ["git", "commit", "-am", "would sign"], cwd=repo, capture_output=True,
        text=True, env=_git_env(),
    )
    assert control.returncode != 0 and "sign" in control.stderr

    assert synthesize_commit(ShellRunner(), repo, base_sha=_head(repo))


def test_synthesis_from_a_subdirectory_still_captures_the_whole_repo(tmp_path):
    """A `git_root` child's path is a subdirectory of its parent's worktree, so
    synthesis may be invoked there. Verified: `git add -A` with no pathspec is
    whole-tree since git 2.0, and `git write-tree` writes the full index."""
    mono = tmp_path / "mono"
    (mono / "pkg").mkdir(parents=True)
    _git("init", "-q", "-b", "main", ".", cwd=mono)
    (mono / "root.txt").write_text("root\n")
    (mono / "pkg" / "c.txt").write_text("child\n")
    _git("add", "-A", cwd=mono)
    _git("commit", "-qm", "init", cwd=mono)
    (mono / "root.txt").write_text("ROOT-EDIT\n")
    (mono / "pkg" / "c.txt").write_text("CHILD-EDIT\n")

    sha = synthesize_commit(ShellRunner(), mono / "pkg", base_sha=_head(mono))

    # Listed from the repo ROOT: `git ls-tree` filters and strips by the cwd
    # prefix, so running it inside `pkg/` would show a single `c.txt` and hide
    # the very regression this test exists for.
    assert sorted(_tree_files(mono, sha)) == ["pkg/c.txt", "root.txt"]
    assert _git("show", f"{sha}:root.txt", cwd=mono) == "ROOT-EDIT"


def test_a_clean_tree_still_synthesizes_the_same_content(repo):
    """Not a path the CLI takes (clean repos go to origin), but the function
    must not depend on there being changes."""
    base = _head(repo)
    sha = synthesize_commit(ShellRunner(), repo, base_sha=base)
    assert _git("rev-parse", f"{sha}^{{tree}}", cwd=repo) == _git(
        "rev-parse", f"{base}^{{tree}}", cwd=repo
    )


def test_a_git_failure_raises_a_named_error(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(RunTransferError) as exc:
        synthesize_commit(ShellRunner(), not_a_repo, base_sha="HEAD")
    assert "read-tree" in str(exc.value)


# --- delivery: where the commit goes, and where the token does NOT -----------

import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mship.core.run_host import RunHostConnection
from mship.core.run_transfer import delete_run_ref, extra_header_env, push_run_ref
from mship.util.shell import ShellResult, ShellRunner


class RecordingShell:
    """Records every command with the env it was given — the only way to assert
    where the bearer did and did not go. `ShellRunner.run` takes a command
    STRING (shell=True), so `calls[i][0]` is the whole command line."""

    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.calls: list[tuple[str, Path, dict]] = []
        self._returncode = returncode
        self._stderr = stderr

    def run(self, command, cwd=None, env=None, **kw):
        self.calls.append((command, Path(cwd), dict(env or {})))
        return ShellResult(returncode=self._returncode, stdout="", stderr=self._stderr)


CONN = RunHostConnection(url="https://mac-abc.relay.example", token="tok-secret")
RECEIVE_URL = "https://mac-abc.relay.example/git/api"


def test_the_push_targets_the_per_task_per_repo_scratch_ref(repo):
    """ac7: two tasks or two repos cannot overwrite each other."""
    shell = RecordingShell()
    ref = push_run_ref(shell, repo, conn=CONN, repo="api", task="t1", sha="abc123")

    assert ref == "refs/mship/run/t1/api"
    command, cwd, _env = shell.calls[0]
    assert "abc123:refs/mship/run/t1/api" in command
    assert cwd == repo


def test_each_run_force_updates_its_own_ref(repo):
    """ac8: the ref is replaced per run — safe only because nothing else writes
    this namespace, which is why it must never be a branch."""
    shell = RecordingShell()
    push_run_ref(shell, repo, conn=CONN, repo="api", task="t1", sha="abc123")
    assert shell.calls[0][0].startswith("git push --force ")


def test_the_push_goes_to_the_run_host_not_origin(repo):
    """ac3: origin is not in this path at all."""
    shell = RecordingShell()
    push_run_ref(shell, repo, conn=CONN, repo="api", task="t1", sha="abc123")
    command = shell.calls[0][0]
    assert RECEIVE_URL in command
    assert "origin" not in command


def test_the_bearer_is_supplied_out_of_band_only(repo):
    """ac4, all three prohibitions at once: not in the remote URL, not written
    to any git config file, and above all not in argv — `/proc/<pid>/cmdline` is
    world-readable, `/proc/<pid>/environ` is not."""
    shell = RecordingShell()
    push_run_ref(shell, repo, conn=CONN, repo="api", task="t1", sha="abc123")
    command, _cwd, env = shell.calls[0]

    assert "tok-secret" not in command
    assert "-c" not in command.split()                    # no `git -c http.extraHeader=`
    assert not any(c[0].startswith("git config") for c in shell.calls)
    assert "Authorization: Bearer tok-secret" in env.values()
    assert f"http.{RECEIVE_URL}.extraHeader" in env.values()
    # The token rides in a VALUE, never in a key: keys are the half of this that
    # a `git config --list`-style dump would show without values.
    assert not any("tok-secret" in key for key in env)


def test_the_push_never_prompts_for_credentials(repo):
    """A stale token must fail the command, not hang the CLI on a prompt that a
    captured-output subprocess never shows anyone."""
    shell = RecordingShell()
    push_run_ref(shell, repo, conn=CONN, repo="api", task="t1", sha="abc123")
    assert shell.calls[0][2]["GIT_TERMINAL_PROMPT"] == "0"


def test_redirects_are_turned_off_for_the_push(repo):
    """The header is bound to the curl handle at request time, NOT re-matched
    per hop, so a same-origin redirect carries it to wherever it points even
    with the key scoped to the run host (verified against real git; see the
    end-to-end test below). Refusing redirects is what actually closes that."""
    shell = RecordingShell()
    push_run_ref(shell, repo, conn=CONN, repo="api", task="t1", sha="abc123")
    env = shell.calls[0][2]
    keys = {env[k]: env[k.replace("KEY", "VALUE")] for k in env if "GIT_CONFIG_KEY" in k}
    assert keys["http.followRedirects"] == "false"


def test_the_header_config_appends_to_inherited_git_config_entries(monkeypatch):
    """tests/conftest.py:41 sets GIT_CONFIG_COUNT=2 suite-wide to disable commit
    signing. Claiming index 0 would silently re-enable signing everywhere."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    env = extra_header_env("tok", RECEIVE_URL)
    assert env["GIT_CONFIG_COUNT"] == "4"
    assert env["GIT_CONFIG_KEY_2"] == f"http.{RECEIVE_URL}.extraHeader"
    assert env["GIT_CONFIG_KEY_3"] == "http.followRedirects"
    assert "GIT_CONFIG_KEY_0" not in env          # the inherited pair is untouched
    assert "GIT_CONFIG_KEY_1" not in env


def test_a_missing_count_starts_at_zero(monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    env = extra_header_env("tok", RECEIVE_URL)
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == f"http.{RECEIVE_URL}.extraHeader"


@pytest.mark.parametrize("count", ["not-a-number", "-1", ""])
def test_a_nonsense_count_does_not_crash_the_push(monkeypatch, count):
    monkeypatch.setenv("GIT_CONFIG_COUNT", count)
    env = extra_header_env("tok", RECEIVE_URL)
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == f"http.{RECEIVE_URL}.extraHeader"


def test_a_failed_push_raises_with_gits_own_message(repo):
    shell = RecordingShell(returncode=1, stderr="fatal: unable to access\n")
    with pytest.raises(RunTransferError) as exc:
        push_run_ref(shell, repo, conn=CONN, repo="api", task="t1", sha="abc123")
    assert "unable to access" in str(exc.value)
    assert "api" in str(exc.value)


def test_delete_uses_the_colon_refspec_form(repo):
    """ac8: cleanup is one plain refspec through the same `_push` as the push —
    no option parsing after the URL.

    NOT because `--delete <ref>` errors on an absent ref: with a fully qualified
    ref (which `run_ref` always builds) both forms are a no-op success, and with
    an unqualified one both fail (git 2.43, verified locally and over HTTP). The
    no-op success cleanup relies on is pinned end to end below."""
    shell = RecordingShell()
    delete_run_ref(shell, repo, conn=CONN, repo="api", task="t1")
    command = shell.calls[0][0]
    assert ":refs/mship/run/t1/api" in command
    assert "--delete" not in command


def test_a_failed_delete_raises(repo):
    shell = RecordingShell(returncode=1, stderr="fatal: unable to access\n")
    with pytest.raises(RunTransferError) as exc:
        delete_run_ref(shell, repo, conn=CONN, repo="api", task="t1")
    assert "unable to access" in str(exc.value)
    assert "refs/mship/run/t1/api" in str(exc.value)


# --- the same guarantees against real git ------------------------------------


def _urlmatch(env: dict[str, str], url: str) -> str:
    """What real git resolves `http.*` to for `url`, given our env config.

    Asserting the key string is not the same as asserting git AGREES the key
    applies: a URL-scoped key that matches nothing would still look right in a
    dict and would silently push unauthenticated."""
    return subprocess.run(
        ["git", "config", "--get-urlmatch", "http", url],
        capture_output=True, text=True, env={**_git_env(), **env},
    ).stdout


def test_real_git_applies_the_header_to_the_run_host_and_nowhere_else(repo):
    """The scoping is only worth having if git reads it the way we mean it."""
    shell = RecordingShell()
    push_run_ref(shell, repo, conn=CONN, repo="api", task="t1", sha="abc123")
    env = shell.calls[0][2]

    # Both legs git will request live under the receive URL.
    for leg in ("/info/refs?service=git-receive-pack", "/git-receive-pack"):
        assert "Bearer tok-secret" in _urlmatch(env, RECEIVE_URL + leg)
    assert "tok-secret" not in _urlmatch(env, "https://elsewhere.example/git/api")
    assert "tok-secret" not in _urlmatch(env, "https://mac-abc.relay.example/git/other")


@contextlib.contextmanager
def _recording_host(redirect: bool = False):
    """A loopback HTTP server that records the Authorization header of every
    request, optionally 302-ing the smart-HTTP entry point somewhere else on the
    SAME origin — the case URL scoping cannot save, because curl only strips a
    custom Authorization header when the redirect crosses origins."""
    seen: list[tuple[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if redirect and self.path.startswith("/git/api/"):
                self.send_response(302)
                self.send_header(
                    "Location", self.path.replace("/git/api/", "/elsewhere/", 1)
                )
            else:
                self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_a_redirect_does_not_carry_the_bearer(repo, monkeypatch):
    """The leak the URL scoping does NOT close. With redirects followed, real
    git sends `Authorization: Bearer …` to the redirect target verbatim
    (verified); `http.followRedirects=false` makes the push fail instead."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    with _recording_host(redirect=True) as (base, seen):
        conn = RunHostConnection(url=base, token="tok-live")
        with pytest.raises(RunTransferError) as exc:
            push_run_ref(
                ShellRunner(), repo, conn=conn, repo="api", task="t1",
                sha=_head(repo),
            )

    assert "302" in str(exc.value)
    assert [path for path, _auth in seen] == [
        "/git/api/info/refs?service=git-receive-pack"
    ], "git followed the redirect"
    assert all("elsewhere" not in path for path, _ in seen)


def test_the_bearer_reaches_the_run_host_over_a_real_socket(repo, monkeypatch):
    """The other half: refusing redirects must not have broken the auth."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    with _recording_host() as (base, seen):
        conn = RunHostConnection(url=base, token="tok-live")
        with pytest.raises(RunTransferError):     # the recorder 404s every path
            push_run_ref(
                ShellRunner(), repo, conn=conn, repo="api", task="t1",
                sha=_head(repo),
            )

    assert seen == [
        ("/git/api/info/refs?service=git-receive-pack", "Bearer tok-live")
    ]


def test_a_real_push_lands_the_ref_and_writes_the_token_to_no_config_file(tmp_path):
    """End to end through the run host's own receive endpoint: the ref lands,
    the bearer only ever travelled in the environment, and — the assertion the
    mocks above cannot make — nothing on disk holds it afterwards.

    Also the live proof that the GIT_CONFIG append is right: tests/conftest.py
    has GIT_CONFIG_COUNT=2 set in `os.environ` for the whole session and
    `ShellRunner` merges `os.environ` under our env, so a push that claimed
    index 0 or miscounted would fail here against real git.
    """
    from tests.core.test_git_receive import _app, _repo, live_serve

    host_repo = _repo(tmp_path / "api")
    operator = tmp_path / "operator"
    subprocess.run(
        ["git", "clone", "-q", str(host_repo), str(operator)],
        check=True, capture_output=True, env=_git_env(),
    )
    (operator / "scratch.txt").write_text("uncommitted\n")
    sha = synthesize_commit(ShellRunner(), operator, base_sha=_head(operator))

    with live_serve(_app(tmp_path, auth_token="tok-abc")) as base:
        conn = RunHostConnection(url=base, token="tok-abc")
        ref = push_run_ref(
            ShellRunner(), operator, conn=conn, repo="api", task="t1", sha=sha
        )
        assert _git("rev-parse", ref, cwd=host_repo) == sha
        assert _git("show", f"{ref}:scratch.txt", cwd=host_repo) == "uncommitted"

        delete_run_ref(ShellRunner(), operator, conn=conn, repo="api", task="t1")
        assert ref not in _git("for-each-ref", "--format=%(refname)", cwd=host_repo)
        # Deleting what is no longer there is a no-op success, so `mship close`
        # never has to ask first.
        delete_run_ref(ShellRunner(), operator, conn=conn, repo="api", task="t1")

    on_disk = "\n".join(
        p.read_text(errors="ignore")
        for p in operator.rglob("*") if p.is_file() and ".git" in p.parts
    )
    assert "tok-abc" not in on_disk
    assert "tok-abc" not in (operator / ".git" / "config").read_text()


# --- cleanup on close --------------------------------------------------------

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.run_host import RunHostStore
from mship.core.run_transfer import cleanup_run_refs


class FakeTask:
    def __init__(self, slug, affected_repos):
        self.slug = slug
        self.affected_repos = affected_repos


def _run_host_config(tmp_path, **extra_repos) -> WorkspaceConfig:
    repos = {"api": RepoConfig(path=tmp_path / "api", type="service", run_host="role-x")}
    repos.update(extra_repos)
    return WorkspaceConfig(workspace="t", run_hosts=["role-x"], repos=repos)


def _store(tmp_path) -> RunHostStore:
    store = RunHostStore(tmp_path / ".mothership")
    store.set("role-x", RunHostConnection(url="http://remote.example", token="tok-abc"))
    return store


def test_close_deletes_the_tasks_scratch_ref(tmp_path):
    """ac8: they do not accumulate."""
    shell = RecordingShell()
    warnings: list[str] = []

    deleted = cleanup_run_refs(
        FakeTask("t1", ["api"]),
        config=_run_host_config(tmp_path), store=_store(tmp_path),
        shell=shell, warn=warnings.append,
    )

    assert deleted == ["api"]
    assert ":refs/mship/run/t1/api" in shell.calls[0][0]
    assert warnings == []


def test_a_git_root_child_is_cleaned_once_via_its_parent(tmp_path):
    """ac7 again: one git repository, one ref, one delete."""
    shell = RecordingShell()
    config = _run_host_config(
        tmp_path,
        server=RepoConfig(path=Path("server"), type="service", git_root="api"),
    )

    deleted = cleanup_run_refs(
        FakeTask("t1", ["api", "server"]), config=config, store=_store(tmp_path),
        shell=shell, warn=lambda _m: None,
    )

    assert deleted == ["api"]
    assert len(shell.calls) == 1


def test_no_mapped_run_host_means_nothing_to_clean(tmp_path):
    """A task that never ran remotely must not warn on every close."""
    shell = RecordingShell()
    warnings: list[str] = []

    deleted = cleanup_run_refs(
        FakeTask("t1", ["api"]),
        config=WorkspaceConfig(
            workspace="t",
            repos={"api": RepoConfig(path=tmp_path / "api", type="service")},
        ),
        store=RunHostStore(tmp_path / ".mothership"),
        shell=shell, warn=warnings.append,
    )

    assert deleted == [] and shell.calls == [] and warnings == []


def test_a_failed_delete_warns_and_keeps_going(tmp_path):
    """Cleanup must never block a close: a missed ref is disk, not disclosure."""
    shell = RecordingShell(returncode=1, stderr="host unreachable\n")
    warnings: list[str] = []

    deleted = cleanup_run_refs(
        FakeTask("t1", ["api"]), config=_run_host_config(tmp_path),
        store=_store(tmp_path), shell=shell, warn=warnings.append,
    )

    assert deleted == []
    assert warnings and "api" in warnings[0]


def test_a_task_slug_that_cannot_form_a_ref_warns_rather_than_raising(tmp_path):
    """`run_ref` refuses `/` in a slug; a close must not die on it."""
    shell = RecordingShell()
    warnings: list[str] = []

    deleted = cleanup_run_refs(
        FakeTask("a/b", ["api"]), config=_run_host_config(tmp_path),
        store=_store(tmp_path), shell=shell, warn=warnings.append,
    )

    assert deleted == [] and shell.calls == []
    assert warnings
