"""Getting a spec's committed evidence onto raw.githubusercontent.com — and
proving it arrived before anyone links to it.

`mship capture --evidence` writes an artifact into the workspace repo's working
tree; the PR body wants to embed it as a sha-pinned raw URL. Between those two
sits a git commit and a push that, until this module owned them, nobody made:
the ordinary sequence (capture, then finish without touching `specs/`) emitted a
URL to an untracked path, which 404s silently. `publish_evidence` is that owner.

It writes to the workspace repo, which nothing else in mship does — `mship sync`
deliberately leaves that repo alone because it holds the operator's config and
prose. The licence here is narrow and stays narrow: only files under
`specs/evidence/<spec-id>/` that a criterion actually references, staged by
explicit pathspec and committed by explicit pathspec, so a partial commit is
made even if the operator has unrelated work staged. Never `commit -a`, never
`add -A`, never `push --force`, never `add -f` past a .gitignore.

Every failure degrades and none of them block: a PR that names its artifact is
worth far more than no PR at all. So each step returns a reason rather than
raising, and the caller turns the reasons into one operator warning.

The storage-mode half of the condition is the CALLER's: this module answers only
the git question ("are these bytes reachable at raw.githubusercontent.com?"), and
`evidence_store.resolve_evidence_mode` already owns the config question. Callers
must not call into here under `local` (gitignored, so never on the remote — and
staging it would be actively wrong) or `encrypted` (on the remote, but as
ciphertext).

The URL pins a commit sha, never a branch: a content-hashed filename at a fixed
sha stays valid forever, whereas a branch link breaks the moment files move.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import NamedTuple

from mship.core.evidence_store import EVIDENCE_DIRNAME, SPECS_DIRNAME
from mship.core.pr import _parse_github_slug

RAW_HOST = "https://raw.githubusercontent.com"

# `mship finish` must never become interactive. A workspace repo whose
# credentials are not cached would otherwise stop mid-finish on a password
# prompt with a PR half-opened, which is worse than a named artifact. Both
# settings make git fail fast instead of asking, and the timeout bounds a push
# that hangs on an unresponsive host. GIT_SSH_COMMAND is left alone when the
# operator already set one — theirs may select a key we know nothing about.
PUSH_TIMEOUT_SECONDS = 120


def _push_env() -> dict[str, str]:
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if not os.environ.get("GIT_SSH_COMMAND"):
        env["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes"
    return env


class EvidencePublication(NamedTuple):
    """What came out fetchable, and what the operator should be told.

    `verified` holds only refs proven present in the pinned commit's tree, so a
    caller embeds per-ref and falls back to naming for the rest.
    """

    base_url: str | None
    verified: frozenset[str]
    warning: str | None


def _stdout(shell, command: str, cwd: Path) -> str | None:
    """Trimmed stdout, or None on any non-zero exit."""
    result = shell.run(command, cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _reason(result) -> str:
    """The last line of stderr — git puts the actionable part there."""
    lines = [ln.strip() for ln in (result.stderr or "").splitlines() if ln.strip()]
    return lines[-1] if lines else "no error output"


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


def _raw_base_and_sha(workspace_root: Path, shell) -> tuple[str, str] | None:
    """The raw base URL and the sha it pins, or None when not fetchable.

    Resolved together in one call so the sha the URL advertises is the exact sha
    the tracked-at check interrogates — re-deriving it would leave a window in
    which the two could disagree.
    """
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
    base = f"{RAW_HOST}/{owner}/{repo}/{sha}/{SPECS_DIRNAME}/{EVIDENCE_DIRNAME}"
    return base, sha


def workspace_raw_base(workspace_root: Path, shell) -> str | None:
    """`https://raw.githubusercontent.com/<owner>/<repo>/<sha>/specs/evidence`,
    or None when the workspace's evidence is not fetchable from there.

    Says nothing about whether any particular artifact is IN that commit — see
    `is_tracked_at`, and prefer `publish_evidence` which does both.
    """
    resolved = _raw_base_and_sha(workspace_root, shell)
    return resolved[0] if resolved else None


def is_tracked_at(shell, workspace_root: Path, sha: str, relpath: str) -> bool:
    """True when `relpath` exists in `sha`'s tree.

    The precondition of every embed, checked by the module that emits the URL
    rather than assumed from the module that wrote the file. Working-tree
    presence is deliberately not consulted: an artifact committed and later
    deleted locally is still fetchable at the pinned sha, and one sitting
    untracked on disk is not.
    """
    result = shell.run(
        f"git cat-file -e {shlex.quote(f'{sha}:{relpath}')}", cwd=Path(workspace_root)
    )
    return result.returncode == 0


def evidence_relpath(spec_id: str, ref: str) -> str:
    """The artifact's path relative to the workspace repo root, in git's own
    forward-slash form (a git pathspec, never an OS path)."""
    return f"{SPECS_DIRNAME}/{EVIDENCE_DIRNAME}/{spec_id}/{ref}"


def _commit_message(spec_id: str, count: int) -> str:
    plural = "artifact" if count == 1 else "artifacts"
    return (
        f"chore(evidence): publish {count} {plural} for {spec_id}\n\n"
        f"Written by `mship capture --evidence` and committed by `mship finish` "
        f"so the PR body can embed them from raw.githubusercontent.com.\n\n"
        f"Scoped to {SPECS_DIRNAME}/{EVIDENCE_DIRNAME}/{spec_id}/ — no other "
        f"path in this repo is staged or committed."
    )


def _commit_artifacts(shell, root: Path, spec_id: str, relpaths: list[str]) -> str | None:
    """Stage and commit exactly `relpaths`. None on success (including nothing
    to do), else a reason.

    Both halves are pathspec-scoped, and the commit is deliberately a PARTIAL
    commit (`git commit -- <paths>`): it takes those paths' working-tree content
    and ignores the rest of the index, so unrelated work the operator had already
    staged is neither committed nor unstaged by us. No `-f`, so an evidence
    directory the operator has gitignored refuses to stage rather than being
    forced past their wishes.
    """
    pathspec = " ".join(shlex.quote(p) for p in relpaths)
    add = shell.run(f"git add -- {pathspec}", cwd=root)
    if add.returncode != 0:
        return f"could not stage the artifacts ({_reason(add)})"
    # Names rather than `--quiet`, so the commit message can say how many
    # artifacts it really carries. Only the COUNT is taken from git's output —
    # the commit still uses the pathspec we built ourselves, so no path ever
    # makes a round trip through git's quoting rules.
    staged = shell.run(f"git diff --cached --name-only -- {pathspec}", cwd=root)
    if staged.returncode != 0:
        return f"could not inspect the staged artifacts ({_reason(staged)})"
    changed = len([line for line in staged.stdout.splitlines() if line.strip()])
    if not changed:
        return None
    message = _commit_message(spec_id, changed)
    commit = shell.run(
        f"git commit -m {shlex.quote(message)} -- {pathspec}", cwd=root
    )
    if commit.returncode != 0:
        return f"could not commit the artifacts ({_reason(commit)})"
    return None


def _push_current_branch(shell, root: Path) -> str | None:
    """Push the workspace repo's current branch to origin. None on success, else
    a reason.

    Only ever updates a branch origin already has: creating one would publish a
    workspace the operator has chosen not to push, which is far beyond the
    licence to commit an artifact. Never `--force`, so a diverged branch is
    reported rather than overwritten, and never `-u`, so the operator's tracking
    config is left as they set it.
    """
    branch = _stdout(shell, "git rev-parse --abbrev-ref HEAD", root)
    if not branch:
        return "the workspace repo has no resolvable HEAD"
    if branch == "HEAD":
        return "the workspace repo is on a detached HEAD"
    ls = shell.run(f"git ls-remote --heads origin {shlex.quote(branch)}", cwd=root)
    if ls.returncode != 0:
        return f"origin is unreachable ({_reason(ls)})"
    if not ls.stdout.strip():
        return f"origin has no {branch!r} branch to update"
    try:
        result = shell.run(
            f"git push origin {shlex.quote(branch)}",
            cwd=root,
            env=_push_env(),
            timeout=PUSH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"the push timed out after {PUSH_TIMEOUT_SECONDS}s"
    if result.returncode != 0:
        return f"the push failed ({_reason(result)})"
    return None


def publish_evidence(
    workspace_root: Path, spec_id: str, refs: list[str], shell
) -> EvidencePublication:
    """Commit and push `spec_id`'s referenced artifacts, then report which of
    them are provably fetchable.

    Call only under `committed` evidence storage (see the module docstring).
    `refs` are bare stored refs; anything not proven present in the pinned
    commit is left out of `verified` so the caller names it instead.
    """
    root = Path(workspace_root)
    relpaths = {ref: evidence_relpath(spec_id, ref) for ref in refs}

    # The licence to write to the operator's workspace repo is "so the PR body
    # can embed this" — so when no embed is possible at all, write nothing. A
    # non-GitHub origin has no raw host to serve from, and committing anyway
    # would be a change to their repo that buys them nothing.
    remote_url = _stdout(shell, "git remote get-url origin", root)
    if (_parse_github_slug(remote_url) if remote_url else None) is None:
        return EvidencePublication(
            None,
            frozenset(),
            _warning(["the workspace repo has no GitHub origin"], spec_id, root),
        )

    # A commit on a detached HEAD would be worse than useless: it is unreachable
    # from any branch, and the next `git checkout` would DELETE the artifact from
    # the working tree (tracked in the commit being left, absent from the one
    # being entered). Nothing is fetchable from a detached HEAD anyway, so refuse
    # before writing rather than after.
    branch = _stdout(shell, "git rev-parse --abbrev-ref HEAD", root)
    if not branch or branch == "HEAD":
        return EvidencePublication(
            None,
            frozenset(),
            _warning(
                ["the workspace repo is on a detached HEAD, so nothing was "
                 "committed or pushed"],
                spec_id,
                root,
            ),
        )

    # Nothing to publish for a ref whose bytes are gone from the working tree;
    # it may still be tracked at HEAD from an earlier finish, which the
    # verification pass below settles.
    to_commit = sorted(p for p in relpaths.values() if (root / p).exists())
    notes: list[str] = []
    if to_commit:
        note = _commit_artifacts(shell, root, spec_id, to_commit)
        if note is not None:
            notes.append(note)

    resolved = _raw_base_and_sha(root, shell)
    if resolved is None:
        # Not on the remote yet (or at all). Try to put it there, then ASK the
        # remote again rather than assuming a zero exit means what we hoped.
        note = _push_current_branch(shell, root)
        if note is not None:
            notes.append(note)
        resolved = _raw_base_and_sha(root, shell)

    if resolved is None:
        notes.append(
            "the workspace evidence is not reachable at raw.githubusercontent.com"
        )
        return EvidencePublication(None, frozenset(), _warning(notes, spec_id, root))

    base_url, sha = resolved
    verified = frozenset(
        ref for ref, p in relpaths.items() if is_tracked_at(shell, root, sha, p)
    )
    missing = len(relpaths) - len(verified)
    if missing:
        notes.append(
            f"{missing} of {len(relpaths)} image artifacts are not tracked at "
            f"workspace commit {sha[:12]}"
        )
    return EvidencePublication(
        base_url, verified, _warning(notes, spec_id, root) if notes else None
    )


def _warning(notes: list[str], spec_id: str, root: Path) -> str:
    """One operator-facing message: what went wrong, what it costs, what to do."""
    return (
        f"Image evidence for {spec_id} is not embeddable ({'; '.join(notes)}), so "
        f"the PR body will name the artifacts instead of emitting images that "
        f"404. Opening the PR is unaffected. Once "
        f"`{SPECS_DIRNAME}/{EVIDENCE_DIRNAME}/{spec_id}/` is committed and pushed "
        f"in {root}, re-run finish to embed them."
    )
