"""The raw base URL under which this workspace's committed evidence is fetchable.

Returns None whenever an embed would break: non-committed storage, a non-GitHub
remote, or an evidence commit that has not been pushed. Callers name the artifact
instead of emitting an image that 404s.

The storage-mode half of that condition is the CALLER's: this module answers only
the git question ("are these bytes reachable at raw.githubusercontent.com?"), and
`evidence_store.resolve_evidence_mode` already owns the config question. Callers
must not call this under `local` (gitignored, so never on the remote) or
`encrypted` (on the remote, but as ciphertext).

The URL pins a commit sha, never a branch: a content-hashed filename at a fixed
sha stays valid forever, whereas a branch link breaks the moment files move.
"""
from __future__ import annotations

import shlex
from pathlib import Path

from mship.core.evidence_store import EVIDENCE_DIRNAME, SPECS_DIRNAME
from mship.core.pr import _parse_github_slug

RAW_HOST = "https://raw.githubusercontent.com"


def _stdout(shell, command: str, cwd: Path) -> str | None:
    """Trimmed stdout, or None on any non-zero exit."""
    result = shell.run(command, cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _head_is_on_origin(shell, repo: Path, sha: str) -> bool:
    """True when `sha` is reachable from origin's copy of the current branch.

    Asks the REMOTE (`git ls-remote`) for the tip rather than trusting
    `origin/<branch>`, because a remote-tracking ref can be arbitrarily stale and
    a stale one would let us claim "pushed" for a commit that only exists locally
    — the one wrong answer here, since it puts a 404 image in a PR body.

    The residual failure mode is the opposite, safe one: if the remote tip is a
    commit this clone has never fetched, `merge-base` cannot judge ancestry and
    we report not-pushed, so the artifact is named even though it may well be on
    the remote. Same for an unreachable remote and for a detached HEAD (no branch
    to ask about).
    """
    branch = _stdout(shell, "git rev-parse --abbrev-ref HEAD", repo)
    if not branch or branch == "HEAD":
        return False
    ls = shell.run(
        f"git ls-remote origin {shlex.quote('refs/heads/' + branch)}", cwd=repo
    )
    if ls.returncode != 0:
        return False
    remote_sha = ""
    for line in ls.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            remote_sha = parts[0].strip()
            break
    if not remote_sha:
        return False
    if remote_sha == sha:
        return True
    ancestry = shell.run(
        f"git merge-base --is-ancestor {shlex.quote(sha)} {shlex.quote(remote_sha)}",
        cwd=repo,
    )
    return ancestry.returncode == 0


def workspace_raw_base(workspace_root: Path, shell) -> str | None:
    """`https://raw.githubusercontent.com/<owner>/<repo>/<sha>/specs/evidence`,
    or None when the workspace's evidence is not fetchable from there."""
    root = Path(workspace_root)
    remote_url = _stdout(shell, "git remote get-url origin", root)
    slug = _parse_github_slug(remote_url) if remote_url else None
    if slug is None:
        return None
    sha = _stdout(shell, "git rev-parse HEAD", root)
    if not sha:
        return None
    if not _head_is_on_origin(shell, root, sha):
        return None
    owner, repo = slug
    return f"{RAW_HOST}/{owner}/{repo}/{sha}/{SPECS_DIRNAME}/{EVIDENCE_DIRNAME}"
