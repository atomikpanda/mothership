"""Make sure a remote run executes the code you are actually looking at.

`--remote` works by having the run host materialize the task's branch **from
origin** (see `remote_exec.materialize_worktree`, which ends in `git reset --hard
origin/<branch>`). Nothing on the caller side ever checked that origin matched the
local worktree, and nothing pushes during development — `git push -u` happens at
`mship finish`. So two things could happen silently:

  * the branch was never pushed at all, and the remote failed with a materialize
    error that pointed at the remote rather than at the real cause; or
  * the branch was pushed at some earlier point, and the remote ran THAT revision.
    The output looked like a real result for code the operator was not editing.

The second is the dangerous one, and it is what this module exists to prevent. It
is deliberately conservative, and every verdict is reached by ASKING ORIGIN rather
than by reading a local remote-tracking ref: `@{u}` is a cached copy that another
machine's push makes stale, and it goes stale in the unsafe direction — it reports
"up to date" for a branch origin has since moved ahead on. One
`git ls-remote origin refs/heads/<branch>` per dispatched repo settles it (the same
pattern, for the same reason, as `evidence_url._remote_tip`).

Against origin's real tip:

  * the branch is **missing from origin, or origin's tip is an ancestor of HEAD** →
    **push**. That is unambiguously what the operator meant, and nothing is lost.
  * origin's tip is **not in HEAD's history** → **refuse**. The run host would
    execute a commit the operator does not have, and no push can fix it: a
    fast-forward from behind is impossible, so attempting one would only produce a
    confusing git error in place of the real remedy (sync, or reset deliberately).
  * a repo with **tracked** modifications → **refuse**, because inventing a commit
    out of someone's work in progress is not a decision this tool should make
    silently;
  * untracked files only → **warn**. They cannot change what the push contains, but
    they also will not exist on the run host, which is worth saying out loud.

Every one of those verdicts is reached by reading the worktree's **HEAD**, and
`push` publishes what they cleared — so HEAD has to *be* the task's branch for the
answer to mean anything. A worktree that is detached, or has some other branch
checked out, would have one commit inspected and a different one published, after
which the run host resets to the published one and executes code nothing here
looked at. So the identity of what is being inspected is established before its
state is (**refuse** if HEAD is not the task branch), and the push then names the
ref that was inspected — `HEAD:refs/heads/<branch>` — rather than resolving the
branch a second time. The guarantee is meant to hold by construction: the commit
that reaches origin is the commit preflight inspected.

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
DIRTY = "uncommitted changes"
UNREADABLE = "unreadable git state"
BEHIND_ORIGIN = "unpulled commits on origin"
MISSING_WORKTREE = "missing worktree"
ORIGIN_UNREACHABLE = "unverified origin"
WRONG_BRANCH = "worktree is not on the task's branch"

_WHY = {
    DIRTY:
        "the run host materializes the task's branch from origin, so it would "
        "run the last PUSHED revision, not what you are editing.",
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
    DIRTY: [
        "Commit and push first:",
        '  mship commit "<what changed>"        # commits across every task repo',
        '  git -C "{path}" push -u origin HEAD',
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
    untracked_only: bool
    needs_push: bool
    push_reason: str | None      # "not on origin" | "ahead of origin" | None


@dataclass(frozen=True)
class Preflight:
    """The verdict. `blocked` is non-empty when the run must not proceed."""
    states: list[RepoState]
    blocked: list[RepoState]
    to_push: list[RepoState]
    untracked: list[RepoState]

    @property
    def ok(self) -> bool:
        return not self.blocked


def _tail(result) -> str:
    """The last line git wrote — where it puts the actionable part."""
    text = (result.stderr or "").strip() or (result.stdout or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else f"exit {result.returncode}"


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


def _inspect_repo(shell, repo: str, path: Path, branch: str) -> RepoState:
    """One repo's verdict. Every path out of here is either a decision or a
    refusal — there is no "could not tell, carry on"."""
    def state(*, blocked=None, detail=None, untracked_only=False,
              needs_push=False, push_reason=None) -> RepoState:
        return RepoState(
            repo=repo, path=path, branch=branch, blocked_reason=blocked,
            detail=detail, untracked_only=untracked_only,
            needs_push=needs_push, push_reason=push_reason,
        )

    if not path.exists():
        return state(blocked=MISSING_WORKTREE, detail=f"{path} does not exist")

    # `git status --porcelain` lists untracked entries as `??` and never lists
    # gitignored files, so an ignored `.venv` does not make a repo look dirty.
    # Untracked files are reported separately because they cannot alter what a
    # push carries — refusing on them would block a run over a stray scratch file.
    status = shell.run("git status --porcelain", cwd=path)
    if status.returncode != 0:
        # Empty stdout from a FAILED status is indistinguishable from a clean
        # tree, so the return code is the only thing separating "nothing to
        # report" from "could not look".
        return state(blocked=UNREADABLE, detail=_tail(status))

    # WHAT is being inspected, before WHAT STATE it is in: every verdict below
    # reads HEAD and `push` publishes what they cleared, so a worktree that is
    # detached or on another branch would have one commit inspected and another
    # published. Asked after `git status` because a path that is not a git
    # worktree at all is UNREADABLE, not "on the wrong branch". `--quiet` makes a
    # detached HEAD exit non-zero with empty output instead of printing an error.
    head_ref = shell.run("git symbolic-ref --quiet HEAD", cwd=path)
    checked_out = (head_ref.stdout or "").strip().removeprefix("refs/heads/")
    if checked_out != branch:
        return state(
            blocked=WRONG_BRANCH,
            detail=f"HEAD is {checked_out or 'detached'}, not {branch}",
        )

    lines = [ln for ln in (status.stdout or "").splitlines() if ln.strip()]
    if any(not ln.startswith("??") for ln in lines):
        return state(blocked=DIRTY)
    untracked_only = bool(lines)

    tip, error = _origin_tip(shell, path, branch)
    if error is not None:
        return state(blocked=ORIGIN_UNREACHABLE, detail=error,
                     untracked_only=untracked_only)
    if tip is None:
        return state(untracked_only=untracked_only,
                     needs_push=True, push_reason="not on origin")

    head = shell.run("git rev-parse HEAD", cwd=path)
    if head.returncode != 0:
        return state(blocked=UNREADABLE, detail=_tail(head),
                     untracked_only=untracked_only)
    if (head.stdout or "").strip() == tip:
        return state(untracked_only=untracked_only)

    # A tip that is an ancestor of HEAD is one a push fast-forwards past. A tip
    # that is not — because origin moved on, or because the branches diverged, or
    # because this clone has never even fetched that commit — is one the run host
    # would execute in place of the operator's HEAD. That is the whole finding.
    ancestor = shell.run(
        f"git merge-base --is-ancestor {shlex.quote(tip)} HEAD", cwd=path
    )
    if ancestor.returncode == 0:
        return state(untracked_only=untracked_only,
                     needs_push=True, push_reason="ahead of origin")
    return state(
        blocked=BEHIND_ORIGIN, untracked_only=untracked_only,
        detail=f"origin/{branch} is at {tip[:12]}, which is not in your history",
    )


def inspect(task, shell, *, repos: list[str] | None = None) -> Preflight:
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
    """
    selected = None if repos is None else set(repos)
    worktrees = task.worktrees or {}
    states = [
        _inspect_repo(shell, repo, Path(raw_path), task.branch)
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
                untracked_only=False, needs_push=False, push_reason=None,
            )
            for repo in sorted(selected - worktrees.keys())
        ]

    return Preflight(
        states=states,
        blocked=[s for s in states if s.blocked_reason is not None],
        to_push=[s for s in states if s.needs_push],
        untracked=[s for s in states if s.untracked_only],
    )


def blocked_message(pre: Preflight) -> str:
    """Why the run was refused, and the commands that unblock it — one section
    per reason, because a task with a dirty repo AND a stale one needs both
    remedies, not whichever happened to be found first."""
    sections = []
    for reason in (DIRTY, WRONG_BRANCH, BEHIND_ORIGIN, MISSING_WORKTREE,
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

    The refspec names the ref that was INSPECTED — `HEAD:refs/heads/<branch>` —
    rather than letting git resolve `<branch>` a second time. `inspect` already
    refuses a worktree whose HEAD is not the task branch, so the two agree; this
    spelling is what makes them agree by construction rather than by that check
    being present. (`git push -u origin <branch>` from a detached HEAD pushes the
    stale local branch ref and reports "Everything up-to-date" — a success for a
    commit nothing verified.)
    """
    pushed: list[str] = []
    for s in pre.to_push:
        refspec = shlex.quote(f"HEAD:refs/heads/{s.branch}")
        result = shell.run(f"git push -u origin {refspec}", cwd=s.path)
        if result.returncode != 0:
            return pushed, f"could not push {s.repo} ({s.push_reason}): {_tail(result)}"
        pushed.append(s.repo)
    return pushed, None
