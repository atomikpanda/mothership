"""Getting a spec's evidence artifacts onto raw.githubusercontent.com — and
proving they arrived before anyone links to them.

`mship capture --evidence` writes an artifact into the machine-local store
(`.mothership/evidence/<spec-id>/`, see evidence_store.py); the PR body wants to
embed it as a sha-pinned raw URL. Between those two sits a publication step that
nobody else makes, and that this module owns.

WHERE it publishes is the whole design. GitHub has no public API for uploading an
image attachment, so an embed must already live at a ref GitHub serves. The bytes
therefore go to an **orphan branch (`mship-evidence`) in the member repo the pull
request targets** — the repo the reviewer already has open:

  * an orphan branch shares no history with the default branch, so binaries never
    enter `main`'s tree and a clone of the product is unaffected;
  * `raw.githubusercontent.com` serves any ref, so no special hosting is needed;
  * every workspace shape (multi-repo, monorepo, single repo) has a member repo
    with a remote, so this assumes no metarepo — the earlier design published to
    the workspace repo, which in a monorepo or single repo IS the product repo,
    and pushed its `main` as a side effect of opening a PR.

That branch, and only that branch, is pushed. Nothing here ever touches `main`,
any other branch, the index, HEAD, or the working tree — see `_publish_commit`
for how the commit is built with plumbing instead of a checkout.

Every failure degrades and none of them block: a PR that names its artifact is
worth far more than no PR at all. So each step returns a reason rather than
raising, and the caller turns the reasons into one operator warning.

The storage-mode half of the condition is the CALLER's: this module answers only
the git question ("are these bytes reachable at raw.githubusercontent.com?"), and
`evidence_store.resolve_evidence_mode` already owns the config question. Callers
must not call into here under `local` (nothing may leave the machine) or
`encrypted` (the bytes are ciphertext, so an embed would render broken).

The URL pins a commit sha, never a branch: a content-hashed filename at a fixed
sha stays valid forever, whereas a branch link breaks the moment the branch moves.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

from mship.core.evidence_store import evidence_dir
from mship.core.gh_auth import git_cred_args
from mship.core.pr import _parse_github_slug

RAW_HOST = "https://raw.githubusercontent.com"

# One branch name for every repo and every spec. Shared, append-only, and named
# so an operator seeing it in the branch list knows what made it.
ORPHAN_BRANCH = "mship-evidence"
ORPHAN_REF = f"refs/heads/{ORPHAN_BRANCH}"

# Files are stored in the tree as `<spec-id>/<ref>`: `ref` is already a content
# hash, so the spec id is the only grouping needed, and it keeps one spec's
# publications legible in a branch shared by all of them.
_TREE_MODE = "100644"

# `mship finish` must never become interactive. A repo whose credentials are not
# cached would otherwise stop mid-finish on a password prompt with a PR
# half-opened, which is worse than a named artifact. Both settings make git fail
# fast instead of asking, and the timeout bounds a transfer that hangs on an
# unresponsive host. GIT_SSH_COMMAND is left alone when the operator already set
# one — theirs may select a key we know nothing about.
PUSH_TIMEOUT_SECONDS = 120


def _remote_env(token: str | None = None) -> dict[str, str]:
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if not os.environ.get("GIT_SSH_COMMAND"):
        env["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes"
    if token:
        _, cred_env = git_cred_args(token)
        env.update(cred_env)
    return env


def _cred_prefix(token: str | None) -> str:
    """The `-c credential....helper=...` global git option, pre-quoted and ready
    to splice in front of a subcommand — empty when there is no token, so the
    no-token path emits the exact command it always has.

    `-c` is a global option, so it MUST precede the subcommand (`git -c ... push
    ...`, never `git push -c ...`); every remote-facing call below builds its
    command string with this prefix immediately after `git `. Mirrors
    `PRManager.push_branch` (core/pr.py), the precedent for this pattern.
    """
    if not token:
        return ""
    args, _ = git_cred_args(token)
    return " ".join(shlex.quote(a) for a in args) + " "


class EvidencePublication(NamedTuple):
    """What came out fetchable, and what the operator should be told.

    `verified` holds only refs proven present in the pinned commit's tree, so a
    caller embeds per-ref and falls back to naming for the rest.
    """

    base_url: str | None
    verified: frozenset[str]
    warning: str | None


def evidence_tree_path(spec_id: str, ref: str) -> str:
    """The artifact's path inside the orphan branch's tree, in git's own
    forward-slash form (a git path, never an OS path)."""
    return f"{spec_id}/{ref}"


def _stdout(shell, command: str, cwd: Path, env=None) -> str | None:
    """Trimmed stdout, or None on any non-zero exit."""
    result = shell.run(command, cwd=cwd, env=env)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _reason(result) -> str:
    """The last line of stderr — git puts the actionable part there."""
    lines = [ln.strip() for ln in (result.stderr or "").splitlines() if ln.strip()]
    return lines[-1] if lines else "no error output"


def _remote_tip(shell, repo: Path, token: str | None = None) -> tuple[str | None, str | None]:
    """`(sha, reason)` for the orphan branch on origin. `(None, None)` means the
    branch does not exist yet — the first-publication case, which is normal and
    not a failure; `(None, reason)` means origin could not be asked."""
    ls = shell.run(
        f"git {_cred_prefix(token)}ls-remote origin {shlex.quote(ORPHAN_REF)}",
        cwd=repo,
        env=_remote_env(token),
    )
    if ls.returncode != 0:
        return None, f"origin is unreachable ({_reason(ls)})"
    for line in ls.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].strip():
            return parts[0].strip(), None
    return None, None


def _fetch_tip(shell, repo: Path, sha: str, token: str | None = None) -> str | None:
    """Make `sha`'s objects available locally so a new commit can PARENT on it.

    Published artifacts accumulate: the new commit extends the existing orphan
    branch rather than replacing it, which needs the old tree readable here. A
    clone that has never fetched the branch (or a tip pushed from another
    machine) has to fetch first. None on success, else a reason.

    `--no-tags` and no refspec destination: the objects land in the object store
    and nothing local is renamed, moved, or created.
    """
    if _stdout(shell, f"git cat-file -e {shlex.quote(sha + '^{commit}')}", repo) is not None:
        return None
    try:
        fetched = shell.run(
            f"git {_cred_prefix(token)}fetch --no-tags --quiet origin {shlex.quote(ORPHAN_REF)}",
            cwd=repo,
            env=_remote_env(token),
            timeout=PUSH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"fetching {ORPHAN_BRANCH} timed out after {PUSH_TIMEOUT_SECONDS}s"
    if fetched.returncode != 0:
        return f"could not fetch the existing {ORPHAN_BRANCH} branch ({_reason(fetched)})"
    if _stdout(shell, f"git cat-file -e {shlex.quote(sha + '^{commit}')}", repo) is None:
        return f"the fetched {ORPHAN_BRANCH} tip {sha[:12]} is still not readable here"
    return None


def _commit_message(spec_id: str, count: int) -> str:
    plural = "artifact" if count == 1 else "artifacts"
    return (
        f"chore(evidence): publish {count} {plural} for {spec_id}\n\n"
        f"Captured by `mship capture --evidence` and published by `mship finish` "
        f"so the PR body can embed them from raw.githubusercontent.com.\n\n"
        f"This is the `{ORPHAN_BRANCH}` orphan branch: it shares no history with "
        f"the default branch and holds nothing but evidence artifacts under "
        f"<spec-id>/."
    )


def _publish_commit(
    shell, repo: Path, spec_id: str, sources: dict[str, Path], parent: str | None
) -> tuple[str | None, str | None]:
    """Build a commit carrying `sources` on top of `parent`. `(sha, reason)`.

    Built entirely with plumbing, because the bytes being published do NOT live
    in this repo's working tree — they sit in the workspace's local evidence
    store, outside the repo entirely. The obvious alternative (check the orphan
    branch out, copy files in, commit) is unavailable: `mship finish` runs while
    an agent is working in this very worktree, and checking out another branch
    would rewrite the files under it.

    So: `hash-object -w` writes each artifact into the object store from an
    arbitrary path on disk; a THROWAWAY index (`GIT_INDEX_FILE`) is seeded from
    the parent commit's tree and added to, so earlier publications survive and
    the repo's real index is never opened; `write-tree` + `commit-tree` produce
    the commit. Nothing here writes a ref, moves HEAD, or touches a file in the
    working tree — the only lasting effect is a few unreferenced objects if the
    push later fails, which git gc collects.

    `--no-filters` on hash-object: these are binaries, and a repo whose
    `.gitattributes` declares CRLF or a clean filter must not be allowed to
    rewrite them into a blob that no longer matches the ref's content hash.
    """
    with tempfile.TemporaryDirectory(prefix="mship-evidence-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if parent is not None:
            seeded = shell.run(f"git read-tree {shlex.quote(parent)}", cwd=repo, env=env)
            if seeded.returncode != 0:
                return None, f"could not read the existing {ORPHAN_BRANCH} tree ({_reason(seeded)})"
        for ref, src in sorted(sources.items()):
            blob = _stdout(
                shell,
                f"git hash-object -w --no-filters {shlex.quote(str(src))}",
                repo,
            )
            if not blob:
                return None, f"could not store {ref} as a git object"
            added = shell.run(
                f"git update-index --add --cacheinfo "
                f"{_TREE_MODE},{blob},{shlex.quote(evidence_tree_path(spec_id, ref))}",
                cwd=repo,
                env=env,
            )
            if added.returncode != 0:
                return None, f"could not add {ref} to the evidence tree ({_reason(added)})"
        tree = _stdout(shell, "git write-tree", repo, env=env)
    if not tree:
        return None, "could not write the evidence tree"

    # Identical content re-published (content-addressed refs make that the norm
    # on a re-run of finish) produces the parent's own tree. Reuse the parent
    # commit rather than stacking an empty one on the operator's branch.
    if parent is not None and _stdout(
        shell, f"git rev-parse {shlex.quote(parent + '^{tree}')}", repo
    ) == tree:
        return parent, None

    message = _commit_message(spec_id, len(sources))
    parent_arg = f"-p {shlex.quote(parent)} " if parent else ""
    commit = shell.run(
        f"git commit-tree {tree} {parent_arg}-m {shlex.quote(message)}", cwd=repo
    )
    if commit.returncode != 0:
        return None, f"could not create the evidence commit ({_reason(commit)})"
    sha = commit.stdout.strip()
    if not sha:
        return None, "could not create the evidence commit"
    return sha, None


def _push_commit(shell, repo: Path, sha: str, token: str | None = None) -> str | None:
    """Push `sha` to the orphan branch. None on success, else a reason.

    Pushes the COMMIT OBJECT (`<sha>:refs/heads/<branch>`) rather than a local
    branch, so publication creates no local ref at all: nothing appears in the
    operator's `git branch`, and a stale local copy can never diverge from the
    remote and start rejecting pushes.

    Never `--force`: the new commit descends from the tip we read, so an ordinary
    fast-forward is all it needs, and a concurrent publication from elsewhere is
    reported rather than overwritten. `refs/heads/<branch>` is spelled in full so
    the refspec can only ever name this one branch.
    """
    try:
        result = shell.run(
            f"git {_cred_prefix(token)}push origin {shlex.quote(sha)}:{shlex.quote(ORPHAN_REF)}",
            cwd=repo,
            env=_remote_env(token),
            timeout=PUSH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"the push timed out after {PUSH_TIMEOUT_SECONDS}s"
    if result.returncode != 0:
        return f"the push failed ({_reason(result)})"
    return None


def _is_on_origin(shell, repo: Path, sha: str, token: str | None = None) -> bool:
    """True when `sha` is reachable from origin's copy of the orphan branch.

    Asks the REMOTE rather than trusting a local ref, because claiming "pushed"
    for a commit that only exists locally is the one wrong answer here: it puts a
    404 image in a PR body. The residual failure mode is the opposite, safe one —
    if the remote tip is a commit this clone has never fetched, `merge-base`
    cannot judge ancestry and we report not-pushed, so the artifact is named even
    though it may well be on the remote.
    """
    tip, _ = _remote_tip(shell, repo, token)
    if tip is None:
        return False
    if tip == sha:
        return True
    ancestry = shell.run(
        f"git merge-base --is-ancestor {shlex.quote(sha)} {shlex.quote(tip)}", cwd=repo
    )
    return ancestry.returncode == 0


def is_present_at(shell, repo: Path, sha: str, tree_path: str) -> bool:
    """True when `tree_path` exists in `sha`'s tree.

    The precondition of every embed, checked by the module that emits the URL
    rather than assumed from the module that wrote the file.
    """
    result = shell.run(
        f"git cat-file -e {shlex.quote(f'{sha}:{tree_path}')}", cwd=Path(repo)
    )
    return result.returncode == 0


def publish_evidence(
    workspace_root: Path, repo_path: Path, spec_id: str, refs: list[str], shell,
    token: str | None = None,
) -> EvidencePublication:
    """Publish `spec_id`'s referenced artifacts to `repo_path`'s orphan evidence
    branch, then report which of them are provably fetchable.

    Call once per repo receiving a pull request (see ac7: every PR is
    self-contained, so a reviewer of one repo's PR needs no access to a sibling),
    and only under `published` evidence storage (see the module docstring).
    `refs` are bare stored refs; anything not proven present in the pinned commit
    is left out of `verified` so the caller names it instead.

    `token`, when given, is spliced onto every git call that reaches the network
    (`ls-remote`, `fetch`, `push`) as the same github.com-scoped credential
    helper `push_branch` uses for the branch push (core/pr.py), so a token-only
    environment (cloud agent, CI, an unattended overnight routine) with no
    cached git credentials can publish evidence exactly as a local operator
    with cached credentials already does. `None` reproduces today's behaviour
    unchanged — the operator's cached credentials, if any, apply as before.
    """
    repo = Path(repo_path)
    store = evidence_dir(workspace_root, spec_id)
    tree_paths = {ref: evidence_tree_path(spec_id, ref) for ref in refs}
    notes: list[str] = []

    # No raw host to serve from means no embed is possible at all, so do no git
    # work: the licence to write anything here is "so the PR body can embed it".
    remote_url = _stdout(shell, "git remote get-url origin", repo)
    slug = _parse_github_slug(remote_url) if remote_url else None
    if slug is None:
        return EvidencePublication(
            None, frozenset(), _warning(["the repo has no GitHub origin"], spec_id, repo)
        )
    owner, name = slug

    tip, tip_note = _remote_tip(shell, repo, token)
    if tip_note is not None:
        return EvidencePublication(
            None, frozenset(), _warning([tip_note], spec_id, repo)
        )
    parent = tip
    if parent is not None:
        note = _fetch_tip(shell, repo, parent, token)
        if note is not None:
            # Without the parent's objects we could only build a commit that
            # REPLACES the branch, discarding earlier publications, and the push
            # would be rejected as a non-fast-forward anyway.
            return EvidencePublication(
                None, frozenset(), _warning([note], spec_id, repo)
            )

    # Bytes gone from the local store are not republishable, but may already be
    # on the branch from an earlier finish — the verification pass settles that.
    sources = {ref: store / ref for ref in refs if (store / ref).is_file()}

    pinned = parent
    if sources:
        pinned, note = _publish_commit(shell, repo, spec_id, sources, parent)
        if note is not None:
            notes.append(note)
            pinned = parent

    if pinned is None:
        notes.append(f"nothing could be published to {ORPHAN_BRANCH}")
        return EvidencePublication(None, frozenset(), _warning(notes, spec_id, repo))

    if pinned != tip:
        note = _push_commit(shell, repo, pinned, token)
        if note is not None:
            notes.append(note)
    if not _is_on_origin(shell, repo, pinned, token):
        notes.append(
            f"the evidence commit is not on origin's {ORPHAN_BRANCH} branch"
        )
        return EvidencePublication(None, frozenset(), _warning(notes, spec_id, repo))

    base_url = f"{RAW_HOST}/{owner}/{name}/{pinned}"
    verified = frozenset(
        ref for ref, p in tree_paths.items() if is_present_at(shell, repo, pinned, p)
    )
    missing = len(tree_paths) - len(verified)
    if missing:
        notes.append(
            f"{missing} of {len(tree_paths)} image artifacts are not present at "
            f"evidence commit {pinned[:12]}"
        )
    return EvidencePublication(
        base_url, verified, _warning(notes, spec_id, repo) if notes else None
    )


def _warning(notes: list[str], spec_id: str, repo: Path) -> str:
    """One operator-facing message: what went wrong, what it costs, what to do.

    The remedy deliberately does NOT say "re-run finish": the acceptance block is
    rendered only while a PR is being CREATED, so a second finish over an
    existing PR finds it and leaves its body exactly as it stands (cli/worktree
    .py). Fix the push before the PR opens, or edit the body afterwards.
    """
    return (
        f"Image evidence for {spec_id} is not embeddable ({'; '.join(notes)}), so "
        f"the PR body will name the artifacts instead of emitting images that "
        f"404. Opening the PR is unaffected. Re-running finish will not re-render "
        f"an existing PR's body: make the `{ORPHAN_BRANCH}` branch in {repo} "
        f"pushable to origin BEFORE the PR is opened, or add the images to the "
        f"body by hand afterwards."
    )
