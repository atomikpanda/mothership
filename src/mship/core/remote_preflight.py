"""Make sure a remote run executes the code you are actually looking at.

`--remote` used to run whatever was on origin, because the run host materialized
the task's branch by fetching it. Nothing on the caller side checked that origin
matched the local worktree, and nothing pushes during development — `git push -u`
happens at `mship finish`. So two things could happen silently:

  * the branch was never pushed at all, and the remote failed with a materialize
    error that pointed at the remote rather than at the real cause; or
  * the branch was pushed at some earlier point, and the remote ran THAT revision.
    The output looked like a real result for code the operator was not editing.

The second is the dangerous one, and it is what this module exists to prevent. It
sorts the task's repos into three outcomes:

  * **REFUSE** — the repo cannot be sent, or sending it would be a lie about what
    the operator meant. See the reason constants below.
  * **TRANSFER** (`Preflight.dirty`) — the working tree differs from HEAD, in any
    way at all. `cli/exec.py` synthesizes a commit from that tree
    (`core/run_transfer.py`) and pushes it STRAIGHT TO THE RUN HOST, onto a ref
    under the throwaway namespace `core/run_ref.py` owns. Origin is never in this
    path: routing uncommitted work through it would publish untracked scratch
    files to a third party, and deleting the ref afterwards would not retract the
    objects. Nothing local is mutated — see `synthesize_commit`.
  * **PUSH** (`Preflight.to_push`) — the tree is clean but origin is missing the
    branch or is behind it. Push the branch to origin exactly as before. There is
    nothing extra to send, so nothing extra happens.

Real history goes to origin; throwaway state goes host to host.

ANY `git status --porcelain` output makes a repo dirty, untracked entries
included. That is not laxity: untracked files are part of what the operator sees,
they now travel only between the operator's own two machines, and a rule that
excluded them would mean they never travel at all.

For a CLEAN repo, and only for a clean repo, the verdict is reached by ASKING
ORIGIN rather than by reading a local remote-tracking ref: `@{u}` is a cached copy
that another machine's push makes stale, and it goes stale in the unsafe direction
— it reports "up to date" for a branch origin has since moved ahead on. One
`git ls-remote origin refs/heads/<branch>` settles it (the same pattern, for the
same reason, as `evidence_url._remote_tip`). Against origin's real tip:

  * the branch is **missing from origin, or origin's tip is an ancestor of HEAD** →
    **push**. That is unambiguously what the operator meant, and nothing is lost.
  * origin's tip is **not in HEAD's history** → **refuse**. The run host would
    execute a commit the operator does not have, and no push can fix it: a
    fast-forward from behind is impossible, so attempting one would only produce a
    confusing git error in place of the real remedy (sync, or reset deliberately).

A DIRTY repo never asks origin at all. The run host materializes the scratch ref
this machine pushed, with no fetch (`remote_exec.materialize_worktree(run_ref=…)`),
so origin's tip cannot change what executes — and a network round trip that cannot
change the outcome is one not worth taking. BEHIND_ORIGIN and ORIGIN_UNREACHABLE
therefore keep their full force on the path where they describe a real hazard, and
are simply unreachable on the path where they would not.

A repo mid-merge, mid-rebase, mid-cherry-pick or with unmerged paths is refused
outright (IN_PROGRESS). The working tree is what gets sent, and in that state it
holds conflict markers and half-applied changes; shipping them produces a remote
failure with no visible relationship to the edit the operator was making. That
check runs BEFORE the branch check on purpose: `git rebase` detaches HEAD, so
checking the branch first would refuse a rebase as WRONG_BRANCH and print a
`git checkout` that abandons it.

Every one of those verdicts is reached by reading the worktree's **HEAD**, and
`push` publishes what they cleared — so HEAD has to *be* the task's branch for the
answer to mean anything. A worktree that is detached, or has some other branch
checked out, would have one commit inspected and a different one published, after
which the run host resets to the published one and executes code nothing here
looked at. So the identity of what is being inspected is established before its
state is (**refuse** if HEAD is not the task branch) — and, because two
separate git calls cannot be made atomic against an outside writer, that
identity check and the sha it certifies are read from the SAME `git rev-parse`
invocation rather than as two calls with a window between them (see
`_inspect_repo` for why: a `git checkout` landing in that window would
otherwise verify one branch and capture a different branch's sha). `inspect`
resolves HEAD to a concrete sha ONCE this way and carries it on `RepoState`
(`head_sha`) rather than re-reading HEAD later. `push` then names that exact
sha in its refspec —
`<sha>:refs/heads/<branch>` — instead of resolving `HEAD` (or the branch) a
second time at push time. That second resolution is not hypothetical: this
workspace runs subagents that commit inside a task's worktree while other
commands are in flight, so a `HEAD:refs/heads/<branch>` refspec computed at push
time can legitimately name a different commit than the one every check above
just cleared. The guarantee is meant to hold by construction: the commit that
reaches origin is the commit preflight inspected — including against a write
that lands in the gap between the two.

That guarantee stops at origin. Once the push lands, the branch on origin is
still a mutable ref: another writer with push access can advance it before the
run host fetches it, and nothing on this machine can see or prevent that — the
mutation happens after preflight has finished, on a different machine's clock.
See "Known limitations" in `docs/remote-run.md` for what would actually close
that gap (the run host materializing an immutable revision, not a branch); it
is not something this module can do from here.

Anything that cannot be determined is refused, not assumed clean: a repo `git
status` fails in, a worktree that is gone, an origin that will not answer. The
whole purpose here is "do not dispatch something unverified", so an unverifiable
repo is the one case that must never pass. Each of those gets its own reason,
because the operator's remedy differs — telling someone with a corrupt repo to
commit their work would be worse than saying nothing.

Only the repos actually being dispatched are checked. Checking a repo the run will
not touch would both block a run over unrelated work in progress and push a repo
the operator never named; within that set, EVERY repo is checked, because a task
has a branch per repo and the run host materializes each one separately.
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Asking origin costs one round trip per dispatched repo. It is bounded because a
# preflight that hangs (an ssh host that never answers, a credential prompt) is a
# worse failure than one that refuses: the operator is left with no run and no
# message. `GIT_TERMINAL_PROMPT=0` makes the credential case fail instead of
# blocking on a prompt that a captured-output subprocess never shows anyone.
REMOTE_QUERY_TIMEOUT_SECONDS = 30
_NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0"}

# Why a repo must not be dispatched. Distinct rather than one "dirty" bucket
# because each has a different remedy, and the message has to name the right one.
IN_PROGRESS = "merge or rebase in progress"
UNREADABLE = "unreadable git state"
BEHIND_ORIGIN = "unpulled commits on origin"
MISSING_WORKTREE = "missing worktree"
ORIGIN_UNREACHABLE = "unverified origin"
WRONG_BRANCH = "worktree is not on the task's branch"

_WHY = {
    IN_PROGRESS:
        "the working tree is what gets sent, and mid-operation it holds conflict "
        "markers and half-applied changes rather than code you meant to run.",
    WRONG_BRANCH:
        "every check here reads the worktree's HEAD, but the run host "
        "materializes the TASK's branch — so what was verified and what would "
        "run are two different commits.",
    UNREADABLE:
        "git could not report the repo's state, so nothing about what the run "
        "host would execute could be verified.",
    BEHIND_ORIGIN:
        "the run host resets to origin's tip, so it would execute a commit you "
        "do not have. Pushing cannot fix this — a fast-forward from behind is "
        "impossible.",
    MISSING_WORKTREE:
        "there is no worktree left to compare against what the run host would "
        "execute.",
    ORIGIN_UNREACHABLE:
        "origin is the only authority on what the run host would check out, and "
        "a local remote-tracking ref is a cache that goes stale silently.",
}

# How to unblock, per reason. A line mentioning `{path}` is a per-repo command and
# is emitted once for each blocked repo (with `{branch}` filled in too); every
# other line is printed as written. That keeps the remedy for a five-repo task from
# collapsing into a command the operator has to rewrite five times by hand.
_FIX = {
    IN_PROGRESS: [
        "Inspect and finish or abandon the operation Git identifies, then re-run:",
        '  git -C "{path}" status                 # what git is in the middle of',
    ],
    WRONG_BRANCH: [
        "Check the task's branch back out, then re-run:",
        '  git -C "{path}" checkout {branch}',
    ],
    UNREADABLE: [
        "Run `git status` in the repo to see what git is unhappy about, then "
        "re-run.",
    ],
    BEHIND_ORIGIN: [
        "Bring your branch up to date, then re-run:",
        '  git -C "{path}" pull --ff-only origin {branch}',
        "...or, if origin's commit is the one you want gone, reset to it "
        "deliberately.",
    ],
    MISSING_WORKTREE: [
        "Restore it, or take it out of the run:",
        "  mship worktrees                      # what each task still has on disk",
        "  mship run --remote --repos <repos>   # scope the run away from it",
    ],
    ORIGIN_UNREACHABLE: [
        "Check connectivity and credentials for this repo's origin, then re-run.",
    ],
}


@dataclass(frozen=True)
class RepoState:
    """What one of a task's repos looks like relative to origin."""
    repo: str
    path: Path
    branch: str
    blocked_reason: str | None   # one of the reason constants, else None
    detail: str | None           # git's own words, for this repo
    dirty: bool                  # working tree differs from HEAD -> TRANSFER
    needs_push: bool
    push_reason: str | None      # "not on origin" | "ahead of origin" | None
    head_sha: str | None = None  # HEAD as resolved during inspect; see `push`
    # The TOP-LEVEL git repo this worktree belongs to. Equal to `repo` except
    # for a `git_root` child, whose tree IS its parent's — so both name the
    # parent and dedupe to one transfer (spec ac7). Resolved in `inspect` from
    # the workspace config; `repo` itself when no config was supplied.
    git_repo: str | None = None


@dataclass(frozen=True)
class Preflight:
    """The verdict. `blocked` is non-empty when the run must not proceed."""
    states: list[RepoState]
    blocked: list[RepoState]
    to_push: list[RepoState]
    dirty: list[RepoState]       # one entry per GIT repo, not per repo name

    @property
    def ok(self) -> bool:
        return not self.blocked


def _tail(result) -> str:
    """The last line git wrote — where it puts the actionable part."""
    text = (result.stderr or "").strip() or (result.stdout or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else f"exit {result.returncode}"


# `git status --porcelain` spells an unmerged path with these two-letter codes
# (verified: a real conflict reports `UU`). They are the only porcelain codes
# that mean "git has not finished", which is why they are singled out rather
# than lumped in with the dirty check below.
_UNMERGED_CODES = ("DD", "AU", "UD", "UA", "DU", "AA", "UU")

# Marker files git leaves in the per-worktree git dir while an operation is
# suspended. A `--no-commit` merge leaves MERGE_HEAD with NOTHING unmerged, so
# the porcelain alone cannot see it — hence the extra look.
_OPERATIONS = (
    (
        "rebase-merge",
        "a rebase is in progress",
        ("rebase --continue",),
        ("rebase --abort",),
    ),
    (
        "rebase-apply",
        "a rebase or `git am` is in progress",
        ("rebase --continue", "am --continue"),
        ("rebase --abort", "am --abort"),
    ),
    (
        "sequencer",
        "a cherry-pick or revert sequence is in progress",
        ("cherry-pick --continue", "revert --continue"),
        ("cherry-pick --abort", "revert --abort"),
    ),
    ("MERGE_HEAD", "a merge is in progress", ("merge --continue",), ("merge --abort",)),
    (
        "CHERRY_PICK_HEAD",
        "a cherry-pick is in progress",
        ("cherry-pick --continue",),
        ("cherry-pick --abort",),
    ),
    ("REVERT_HEAD", "a revert is in progress", ("revert --continue",), ("revert --abort",)),
    (
        "BISECT_START",
        "a bisect is in progress",
        ("bisect skip",),
        ("bisect reset",),
    ),
)


def _operation_in_progress(
    git_dir: Path,
) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    """Description and complete recovery commands for a suspended git operation."""
    for marker, description, continue_commands, abort_commands in _OPERATIONS:
        marker_path = git_dir / marker
        if (
            marker == "BISECT_START"
            and marker_path.is_file()
            and marker_path.stat().st_size > 0
        ) or (marker != "BISECT_START" and marker_path.exists()):
            return description, continue_commands, abort_commands
    return None


def _origin_tip(shell, path: Path, branch: str) -> tuple[str | None, str | None]:
    """`(sha, error)` for `branch` on origin, asked of ORIGIN ITSELF.

    `(None, None)` means origin simply does not have the branch — the normal
    state of a task branch before `mship finish`, not a failure.
    """
    ref = f"refs/heads/{branch}"
    try:
        ls = shell.run(
            f"git ls-remote origin {shlex.quote(ref)}",
            cwd=path, env=_NO_PROMPT, timeout=REMOTE_QUERY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, f"origin did not answer within {REMOTE_QUERY_TIMEOUT_SECONDS}s"
    if ls.returncode != 0:
        return None, _tail(ls)
    for line in (ls.stdout or "").splitlines():
        sha, _, name = line.partition("\t")
        if sha.strip() and name.strip() == ref:
            return sha.strip(), None
    return None, None


def _inspect_repo(
    shell, repo: str, path: Path, branch: str, *, git_repo: str | None = None,
) -> RepoState:
    """One repo's verdict. Every path out of here is either a decision or a
    refusal — there is no "could not tell, carry on"."""
    def state(*, blocked=None, detail=None, dirty=False,
              needs_push=False, push_reason=None, head_sha=None) -> RepoState:
        return RepoState(
            repo=repo, path=path, branch=branch, blocked_reason=blocked,
            detail=detail, dirty=dirty,
            needs_push=needs_push, push_reason=push_reason, head_sha=head_sha,
            git_repo=git_repo or repo,
        )

    if not path.exists():
        return state(blocked=MISSING_WORKTREE, detail=f"{path} does not exist")

    # `git status --porcelain` lists untracked entries as `??` and never lists
    # gitignored files, so an ignored `.venv` does not make a repo look dirty.
    # (It DOES list a tracked-and-gitignored file that changed, as ` M` —
    # verified — which is right: that file is part of what gets sent.)
    status = shell.run("git status --porcelain", cwd=path)
    if status.returncode != 0:
        # Empty stdout from a FAILED status is indistinguishable from a clean
        # tree, so the return code is the only thing separating "nothing to
        # report" from "could not look".
        return state(blocked=UNREADABLE, detail=_tail(status))

    lines = [ln for ln in (status.stdout or "").splitlines() if ln.strip()]

    # An interrupted operation is refused BEFORE the branch is checked, because
    # `git rebase` detaches HEAD: checking the branch first would refuse a
    # rebase as WRONG_BRANCH and hand the operator a `git checkout` that
    # abandons it. Still AFTER `git status`, so a path that is not a git
    # worktree at all stays UNREADABLE rather than becoming "finish your merge".
    unmerged = [ln[3:].strip() for ln in lines if ln[:2] in _UNMERGED_CODES]
    git_dir_result = shell.run("git rev-parse --git-dir", cwd=path)
    git_dir_raw = (git_dir_result.stdout or "").strip()
    if git_dir_result.returncode != 0 or not git_dir_raw:
        # Same rule as the status guard above: a repo whose state could not be
        # established is refused, never assumed clean — and never shipped.
        return state(blocked=UNREADABLE, detail=_tail(git_dir_result))
    git_dir = Path(git_dir_raw)
    if not git_dir.is_absolute():
        # `git rev-parse --git-dir` answers a bare `.git` at a repo root and an
        # absolute path in a linked worktree (verified) — resolve either.
        git_dir = path / git_dir
    operation = _operation_in_progress(git_dir)
    if operation is not None or unmerged:
        detail = "unmerged paths"
        if operation is not None:
            description, continue_commands, abort_commands = operation
            detail = "\n".join(
                [description]
                + [
                    f'continue: git -C "{path}" {command}'
                    for command in continue_commands
                ]
                + [
                    f'abort: git -C "{path}" {command}'
                    for command in abort_commands
                ]
            )
        if unmerged:
            detail = f"{detail}\nunresolved: {', '.join(sorted(unmerged)[:5])}"
        return state(blocked=IN_PROGRESS, detail=detail)

    # WHAT is being inspected, before WHAT STATE it is in: every verdict below
    # reads HEAD and `push` publishes what they cleared, so a worktree that is
    # detached or on another branch would have one commit inspected and another
    # published. Asked after `git status` because a path that is not a git
    # worktree at all is UNREADABLE, not "on the wrong branch".
    #
    # Branch identity and the sha to carry forward used to be two separate git
    # invocations — a `symbolic-ref` to name the checked-out branch, then a
    # `rev-parse HEAD` to capture the sha — with a window between them: a `git
    # checkout` landing in that window verifies branch A and then captures a
    # sha that belongs to branch B, so every check past this point reasons
    # about a commit that was never on the branch it validated. Two subprocess
    # calls cannot be made atomic against an outside writer, so instead they
    # are made self-verifying: a SINGLE `git rev-parse HEAD refs/heads/<branch>`
    # reads both shas out of the same process. If they agree, HEAD was at that
    # branch's tip at this instant — exactly the invariant every check below
    # assumes. If they disagree (including one of them failing to resolve at
    # all — a detached HEAD, no local ref by this name, an unborn branch),
    # HEAD is not on the task's branch, which is already a refusal. That
    # subsumes the old `symbolic-ref` check entirely: the two values could
    # never come out equal without HEAD being ON refs/heads/<branch>.
    #
    # One process is not one instant, though: git still resolves HEAD and
    # then refs/heads/<branch> as two sequential reads within this single
    # command, so a ref mutating in THAT window (a concurrent commit, or a
    # force-checkout that also moves the branch) is a torn read too, not just
    # the two-command case above. It needs no separate handling because the
    # same equality check already covers it: either the mutation lands
    # outside the window and both reads see one consistent value, or it lands
    # inside and the two reads disagree — which is the WRONG_BRANCH refusal
    # already taken above. There is no interleaving where the two agree on a
    # sha that was never actually the branch's tip at some consistent instant.
    ref = f"refs/heads/{branch}"
    pair = shell.run(f"git rev-parse HEAD {shlex.quote(ref)}", cwd=path)
    out = (pair.stdout or "").splitlines()
    head_sha = out[0].strip() if out else ""
    branch_tip = out[1].strip() if len(out) > 1 else ""
    if pair.returncode != 0 or not head_sha or head_sha != branch_tip:
        # `symbolic-ref` is asked here ONLY to name what IS checked out, for
        # the refusal message — it plays no part in the decision above, so its
        # being a second, unsynchronized read does not reopen the race: the
        # run is already refused, and no `head_sha` is ever produced for this
        # repo.
        head_ref = shell.run("git symbolic-ref --quiet HEAD", cwd=path)
        checked_out = (head_ref.stdout or "").strip().removeprefix("refs/heads/")
        return state(
            blocked=WRONG_BRANCH,
            detail=f"HEAD is {checked_out or 'detached'}, not {branch}",
        )

    # ANY porcelain output at all — tracked edits, untracked files, or both —
    # means the working tree is not HEAD, and the working tree is what gets
    # sent. Returning HERE is also what keeps origin out of the dirty path: the
    # `ls-remote` below is never reached, because origin's tip cannot change
    # what the run host executes once it is materializing a scratch ref.
    if lines:
        return state(dirty=True, head_sha=head_sha)

    # `head_sha`, resolved above, is carried on every state this repo can
    # still reach rather than re-read at push time. `push` names this exact
    # sha in its refspec instead of the bare branch, which is what git resolves
    # HEAD against a SECOND time — so anything that commits in this worktree
    # between `inspect` and `push` (a subagent, a background job) would otherwise
    # publish a commit nothing here inspected.

    tip, error = _origin_tip(shell, path, branch)
    if error is not None:
        return state(blocked=ORIGIN_UNREACHABLE, detail=error, head_sha=head_sha)
    if tip is None:
        return state(head_sha=head_sha, needs_push=True,
                     push_reason="not on origin")
    if head_sha == tip:
        return state(head_sha=head_sha)

    # A tip that is an ancestor of HEAD is one a push fast-forwards past. A tip
    # that is not — because origin moved on, or because the branches diverged, or
    # because this clone has never even fetched that commit — is one the run host
    # would execute in place of the operator's HEAD. That is the whole finding.
    ancestor = shell.run(
        f"git merge-base --is-ancestor {shlex.quote(tip)} {shlex.quote(head_sha)}",
        cwd=path,
    )
    if ancestor.returncode == 0:
        return state(head_sha=head_sha, needs_push=True,
                     push_reason="ahead of origin")
    return state(
        blocked=BEHIND_ORIGIN, head_sha=head_sha,
        detail=f"origin/{branch} is at {tip[:12]}, which is not in your history",
    )


def inspect(task, shell, *, repos: list[str] | None = None, config=None) -> Preflight:
    """Read the task's worktrees for the repos being dispatched. No mutation.

    `repos` is the caller's `--repos`/`--tag`-filtered set — the repos the run
    will actually touch. Inspecting more than that both refuses runs over
    unrelated work in progress and pushes repos the operator never named. `None`
    means every repo the task touches. A selected repo with no worktree entry on
    the task at all (outside `task.worktrees`) still gets a MISSING_WORKTREE
    refusal, exactly like one whose worktree existed and vanished — the run host
    would materialize it from origin either way.

    Costs one `ls-remote` per selected repo (see the module docstring): a single
    ref, immediately before a network dispatch that makes the run host fetch that
    same branch, so the round trip buys the one guarantee the whole feature rests
    on.

    `config` (the workspace's `WorkspaceConfig`, optional) is used for one thing:
    resolving each repo to its TOP-LEVEL git repo, so a `git_root` child and its
    parent — one git repository, one tree — dedupe to a single entry in `dirty`
    (spec ac7). Without it every repo is its own git repo, which is correct for
    every workspace that has no `git_root` children.
    """
    selected = None if repos is None else set(repos)
    worktrees = task.worktrees or {}
    repo_configs = getattr(config, "repos", {}) if config is not None else {}

    def _git_repo_of(repo: str) -> str:
        rc = repo_configs.get(repo)
        return rc.git_root if rc is not None and rc.git_root else repo

    states = [
        _inspect_repo(shell, repo, Path(raw_path), task.branch,
                      git_repo=_git_repo_of(repo))
        for repo, raw_path in sorted(worktrees.items())
        if selected is None or repo in selected
    ]

    # A selected repo the task has no worktree for at all — e.g. `--repos web`
    # naming a repo `_resolve_repos` never intersected with the task's own repo
    # list — is filtered OUT of `worktrees.items()` above by construction, not
    # blocked: it would otherwise vanish from `states` with no refusal, while the
    # run host materializes it from origin regardless. Same finding as a worktree
    # that existed and then disappeared, so it gets the same MISSING_WORKTREE
    # refusal, not a new mechanism.
    if selected is not None:
        states += [
            RepoState(
                repo=repo, path=Path(f"<no worktree for {repo!r} on this task>"),
                branch=task.branch, blocked_reason=MISSING_WORKTREE,
                detail=f"{repo!r} is not one of this task's repos (no worktree entry)",
                dirty=False, needs_push=False, push_reason=None,
                git_repo=_git_repo_of(repo),
            )
            for repo in sorted(selected - worktrees.keys())
        ]

    # One transfer per GIT repository: a `git_root` child and its parent share
    # one tree, so pushing both would send the same objects twice under two
    # names, and the run host materializes the parent either way (spec ac7).
    # The parent's own state is preferred as the survivor when it is in scope,
    # because the parent is the name the run host resolves the child under.
    dirty_states = [s for s in states if s.dirty]
    dirty: list[RepoState] = []
    seen_git_repos: set[str] = set()
    for s in [d for d in dirty_states if d.repo == d.git_repo] + dirty_states:
        if s.git_repo in seen_git_repos:
            continue
        seen_git_repos.add(s.git_repo)
        dirty.append(s)

    return Preflight(
        states=states,
        blocked=[s for s in states if s.blocked_reason is not None],
        to_push=[s for s in states if s.needs_push],
        dirty=dirty,
    )


def blocked_message(pre: Preflight) -> str:
    """Why the run was refused, and the commands that unblock it — one section
    per reason, because a task with a conflicted repo AND a stale one needs both
    remedies, not whichever happened to be found first."""
    sections = []
    for reason in (IN_PROGRESS, WRONG_BRANCH, BEHIND_ORIGIN, MISSING_WORKTREE,
                   UNREADABLE, ORIGIN_UNREACHABLE):
        group = [s for s in pre.blocked if s.blocked_reason == reason]
        if not group:
            continue
        repos = ", ".join(s.repo for s in group)
        lines = [f"{reason} in {repos} — {_WHY[reason]}", ""]
        details = [f"  {s.repo}: {s.detail}" for s in group if s.detail]
        lines.extend(details + [""] if details else [])
        for fix in _FIX[reason]:
            if "{path}" in fix:
                lines.extend(
                    fix.format(path=s.path, branch=s.branch) for s in group
                )
            else:
                lines.append(fix)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def push(pre: Preflight, shell) -> tuple[list[str], str | None]:
    """Push every repo origin is missing or behind on.

    Returns `(pushed_repos, error)`. A push failure stops the run: proceeding
    after a failed push is exactly the silent-stale-code case this module exists
    to prevent.

    The refspec names the exact sha `inspect` resolved HEAD to —
    `<sha>:refs/heads/<branch>` — rather than letting git resolve `HEAD` (or
    `<branch>`) a second time here. `inspect` already refuses a worktree whose
    HEAD is not the task branch, so the two agree on WHICH ref; spelling the sha
    is what makes them agree on WHICH COMMIT even if something commits in this
    worktree between the two calls — a subagent, a background job — rather than
    relying on nothing else touching the worktree meanwhile. (`git push -u origin
    <branch>` from a detached HEAD pushes the stale local branch ref and reports
    "Everything up-to-date" — a success for a commit nothing verified.)

    That is the last thing this module controls. Once this returns, origin has
    the inspected commit under a mutable branch name, and a concurrent writer can
    move that name again before the run host fetches it. Do not try to close that
    here with a lock, a re-check, or a retry — the mutation happens on a different
    machine, after this function has already returned, so nothing on this side
    can observe or prevent it. See "Before it dispatches" in
    `docs/remote-run.md` for what actually would (the run host resolving an
    immutable revision instead of a branch).
    """
    pushed: list[str] = []
    for s in pre.to_push:
        refspec = shlex.quote(f"{s.head_sha}:refs/heads/{s.branch}")
        result = shell.run(f"git push -u origin {refspec}", cwd=s.path)
        if result.returncode != 0:
            return pushed, f"could not push {s.repo} ({s.push_reason}): {_tail(result)}"
        pushed.append(s.repo)
    return pushed, None
