"""Client side of an exact-copy remote run: turn a working tree into a commit,
and hand that commit to the run host.

Real history goes to origin; throwaway state goes host to host and never touches
origin. This module owns the second half of that rule. `core/remote_preflight.py`
decides which repos take which path; this one carries them.
"""
from __future__ import annotations

import shlex
import tempfile
from pathlib import Path

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
