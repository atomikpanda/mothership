"""Client side of an exact-copy remote run: turn a working tree into a commit,
and hand that commit to the run host.

Real history goes to origin; throwaway state goes host to host and never touches
origin. This module owns the second half of that rule. `core/remote_preflight.py`
decides which repos take which path; this one carries them.
"""
from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

from mship.core.run_ref import run_ref

# Pinned identity for synthesized commits. Deliberately NOT the operator's: this
# is machinery, not a commit they made (spec ac13), and pinning it also means
# synthesis works in a repo with no `user.email` configured.
_IDENTITY_NAME = "mship run"
_IDENTITY_EMAIL = "mship-run@localhost"

_MESSAGE = "mship --remote: working-tree snapshot (throwaway, not real history)"


class RunTransferError(Exception):
    """A git command needed to synthesize or deliver the working tree failed.

    Always names the command and git's own stderr: using git's transport rather
    than a hand-rolled one is partly FOR those diagnostics, so they are passed
    through rather than replaced.
    """


def _checked(shell, command: str, cwd: Path, env: dict[str, str]) -> str:
    result = shell.run(command, cwd=cwd, env=env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RunTransferError(
            f"`{command}` failed in {cwd}: {detail or f'exit {result.returncode}'}"
        )
    return result.stdout or ""


def synthesize_commit(shell, repo_root: Path, *, base_sha: str) -> str:
    """A real commit object whose tree is byte-identical to the working tree at
    `repo_root`, created without touching the repository's own state.

    The TEMPORARY INDEX is load-bearing, not an implementation detail. `git add
    -A` against the DEFAULT index would stage the operator's work in progress —
    a destructive surprise on their real repository — so every command here runs
    with `GIT_INDEX_FILE` pointed at a scratch file that is deleted afterwards.
    HEAD, the current branch, the real index and `git status` output are all
    unchanged when this returns (verified against real git), and the commit
    belongs to no branch.

    `git read-tree <base_sha>` seeds the scratch index BEFORE `git add -A`.
    Without that seed a file that is both tracked and gitignored is silently
    dropped from the tree — git will not add an ignored path that is not already
    in the index — which is exactly the "the remote ran something subtly
    different" failure this feature exists to remove. Verified against real git.

    `base_sha` is the sha `remote_preflight.inspect` certified HEAD to be at, not
    the string `HEAD`. Re-resolving HEAD here would let anything that commits in
    this worktree between inspection and synthesis (a subagent, a background job)
    re-root the snapshot on a commit nothing verified — the same bypass
    `remote_preflight.push` closes for the origin path.

    `git commit-tree` rather than `git commit`: it writes an object and moves no
    ref, and (verified) it does not honour `commit.gpgsign`, so an operator with
    commit signing configured cannot be blocked on a passphrase prompt a
    captured-output subprocess would never show them.

    Untracked files are included — they are part of what the operator sees, and
    they now travel only between the operator's own two machines. Gitignored
    files are not; `git add -A` never picks them up.

    Safe to call from a SUBDIRECTORY of the repository (a `git_root` child):
    `git add -A` with no pathspec is whole-tree since git 2.0 and `git
    write-tree` writes the full index, so the result is the same tree either
    way. Verified.
    """
    with tempfile.TemporaryDirectory(prefix="mship-run-index-") as tmp:
        env = {
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
            "GIT_AUTHOR_NAME": _IDENTITY_NAME,
            "GIT_AUTHOR_EMAIL": _IDENTITY_EMAIL,
            "GIT_COMMITTER_NAME": _IDENTITY_NAME,
            "GIT_COMMITTER_EMAIL": _IDENTITY_EMAIL,
        }
        base = shlex.quote(base_sha)
        _checked(shell, f"git read-tree {base}", repo_root, env)
        _checked(shell, "git add -A", repo_root, env)
        tree = _checked(shell, "git write-tree", repo_root, env).strip()
        sha = _checked(
            shell,
            f"git commit-tree {shlex.quote(tree)} -p {base} "
            f"-m {shlex.quote(_MESSAGE)}",
            repo_root, env,
        ).strip()
    return sha


def extra_header_env(token: str, url: str) -> dict[str, str]:
    """Env that makes git send `Authorization: Bearer <token>` on BOTH legs of a
    push to `url` (the `info/refs` GET and the `git-receive-pack` POST), and
    nowhere else.

    Carried as git's ENV-based config (`GIT_CONFIG_COUNT` / `_KEY_n` /
    `_VALUE_n`, git >= 2.31) rather than `git -c http.extraHeader=…`: argv is
    world-readable through `/proc/<pid>/cmdline`, a process's environment is
    not. Nothing is written to any git config file, and the token never appears
    in the remote URL (spec ac4).

    APPENDS at the next free index instead of claiming index 0 — the caller's
    environment may already carry GIT_CONFIG entries (the test suite disables
    commit signing that way, tests/conftest.py), and overwriting them would
    silently drop them. The index comes from `os.environ` because `ShellRunner.
    run` layers this dict OVER `os.environ`; the two have to be read from the
    same place or the count will not match the keys git actually receives.

    Two settings, both load-bearing:

    - `http.<url>.extraHeader` is SCOPED to the run host, so the bearer cannot
      ride a request this git process makes to anything else.
    - `http.followRedirects=false` because that scoping is not enough on its
      own. git binds the header to the request before it is sent and does not
      re-match config per hop, so a redirect to another path on the SAME origin
      carries the bearer verbatim (verified against real git; curl only strips a
      custom Authorization header when the redirect crosses origins). git's
      default, `initial`, follows exactly the redirect that matters here — the
      one on `info/refs`. Refusing it fails the push loudly instead.

    `GIT_TERMINAL_PROMPT=0` makes a rejected push FAIL rather than block on an
    interactive credential prompt that a captured-output subprocess would never
    show anyone.
    """
    settings = {
        f"http.{url}.extraHeader": f"Authorization: Bearer {token}",
        "http.followRedirects": "false",
    }
    try:
        index = max(int(os.environ.get("GIT_CONFIG_COUNT", "0")), 0)
    except ValueError:
        index = 0
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": str(index + len(settings))}
    for offset, (key, value) in enumerate(settings.items()):
        env[f"GIT_CONFIG_KEY_{index + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{index + offset}"] = value
    return env


def _receive_url(conn, repo: str) -> str:
    """The run host's scoped receive endpoint for `repo`. git appends
    `/info/refs?service=git-receive-pack` and `/git-receive-pack` itself.

    The trailing slash is stripped because this string is both the remote git is
    given AND the URL the auth header is scoped to; a `//` in the middle would
    make the two disagree.
    """
    return f"{conn.url.rstrip('/')}/git/{repo}"


def _push(shell, repo_root: Path, *, conn, refspec: str, url: str, failure: str) -> None:
    result = shell.run(
        f"git push --force {shlex.quote(url)} {refspec}",
        cwd=repo_root, env=extra_header_env(conn.token, url),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RunTransferError(f"{failure}: {detail or f'exit {result.returncode}'}")


def push_run_ref(shell, repo_root: Path, *, conn, repo: str, task: str, sha: str) -> str:
    """Push `sha` straight to the run host's scratch ref, and return that ref.

    Origin is not in this path: uncommitted work — including untracked scratch
    files — goes only between the operator's own two machines.

    `git push` performs the have/want negotiation itself, so only objects the
    run host is missing cross the wire, with no need to compute what it already
    has. `--force` is required because each run replaces the last, and is safe
    precisely because nothing else writes this namespace.

    `repo` is the TOP-LEVEL git repo's name — the receive endpoint refuses a
    `git_root` child, which has no git directory of its own.
    """
    ref = run_ref(task, repo)
    url = _receive_url(conn, repo)
    _push(
        shell, repo_root, conn=conn, url=url,
        refspec=f"{shlex.quote(sha)}:{ref}",
        failure=f"could not send {repo}'s working tree to the run host at {url}",
    )
    return ref


def delete_run_ref(shell, repo_root: Path, *, conn, repo: str, task: str) -> None:
    """Delete this task's scratch ref from the run host.

    Deleting a ref that is not there exits 0 with `remote: warning: deleting a
    non-existent ref`, so `mship close` needs no "does it exist" probe. What
    buys that is the ref being FULLY QUALIFIED, which `run_ref` guarantees: an
    unqualified name has to be resolved against the remote's advertisement and
    fails with `unable to delete 'api': remote ref does not exist` — in the
    `:<ref>` form exactly as in `--delete <ref>` (git 2.43, verified in both
    forms and both transports; the two forms are NOT the distinction, the
    earlier note in this feature's tests notwithstanding).

    The plain `:<ref>` refspec is used anyway so delete and push are the same
    command shape through `_push`, with no option parsing after the URL.
    """
    ref = run_ref(task, repo)
    url = _receive_url(conn, repo)
    _push(
        shell, repo_root, conn=conn, url=url, refspec=f":{ref}",
        failure=f"could not delete {ref} from the run host at {url}",
    )
