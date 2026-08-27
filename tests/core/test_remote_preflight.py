"""The remote-run preflight: never dispatch a run that would execute stale code.

Two kinds of test here, deliberately:

  * scripted-shell tests for the decision table (what blocks, what pushes, what
    only warns), where a fake shell keeps each case to one line; and
  * REAL git repositories with a REAL bare origin for the origin-comparison
    cases. A mocked shell cannot demonstrate that a remote-tracking ref has gone
    stale — staleness is precisely the thing a mock has no notion of — so the
    finding those tests exist for would be untestable without them.
"""
import os
import subprocess
from pathlib import Path

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.remote_preflight import (
    BEHIND_ORIGIN,
    IN_PROGRESS,
    MISSING_WORKTREE,
    ORIGIN_UNREACHABLE,
    UNREADABLE,
    WRONG_BRANCH,
    blocked_message,
    inspect,
    push,
)


class FakeTask:
    def __init__(self, worktrees, branch="feat/x"):
        self.worktrees = worktrees
        self.branch = branch


class FakeShell:
    """Answers git queries from a per-repo script keyed by path name.

    `origin` is the sha `ls-remote` reports for the branch (None = origin does
    not have it); `head` is this worktree's HEAD, reported by the atomic
    `rev-parse HEAD refs/heads/<branch>` pair-read; `contains` lists the shas
    that are ancestors of HEAD, which is how `merge-base --is-ancestor` is
    answered; `head_ref` is what `symbolic-ref HEAD` reports (default: the
    task's branch — `""` with a non-zero rc is a detached HEAD) AND, via the
    pair-read, what decides whether the branch-ref half of that read matches
    `head` or reports `other_branch_sha` instead. `stale_bare_head` answers a
    bare, argument-less `rev-parse HEAD` — a call current code never makes,
    kept only so a reverted (pre-fix) `_inspect_repo` can be exercised in the
    race test's red-before-green check. `pair_output` (with optional
    `pair_rc`) overrides the pair-read's stdout VERBATIM instead of deriving
    it from `head`/`head_ref` — the only way to hand back a genuinely TORN
    pair (two lines that disagree because a ref mutated between git reading
    them, not because the worktree is really on some other branch).
    """

    def __init__(self, answers, push_rc=0, push_err=""):
        self._answers = answers
        self._push_rc = push_rc
        self._push_err = push_err
        self.pushes: list[tuple[str, Path]] = []
        self.touched: set[str] = set()
        self.pair_calls: dict[str, int] = {}
        # Every command issued, in order — the only way to assert that a query
        # was NOT made (e.g. that a dirty repo never asks origin).
        self.commands: list[str] = []

    def run(self, cmd, cwd=None, **kw):
        key = Path(cwd).name
        self.commands.append(cmd)
        spec = self._answers[key]
        self.touched.add(key)
        err = ""
        if "status --porcelain" in cmd:
            out, rc = spec["status"], spec.get("status_rc", 0)
            err = spec.get("status_err", "")
        elif "symbolic-ref" in cmd:
            out = spec.get("head_ref", "refs/heads/feat/x")
            rc = 0 if out else 1
        elif "ls-remote" in cmd:
            if spec.get("ls_remote_rc"):
                out, rc, err = "", spec["ls_remote_rc"], spec.get("ls_remote_err", "")
            elif spec.get("origin") is None:
                out, rc = "", 0
            else:
                out, rc = f"{spec['origin']}\trefs/heads/feat/x\n", 0
        elif "rev-parse --git-dir" in cmd:
            # `_inspect_repo` asks for the per-worktree git dir to look for an
            # interrupted merge/rebase (MERGE_HEAD, rebase-merge/, ...). Real
            # git returns an absolute path in a linked worktree and may return a
            # bare `.git` at a repo root; either is fine here because the
            # production code resolves a relative answer against `cwd`.
            out, rc = spec.get("git_dir", str(Path(cwd) / ".git")), 0
        elif "rev-parse HEAD" in cmd and "refs/heads/" in cmd:
            # The atomic branch-identity + sha read `_inspect_repo` makes:
            # one call answering both "which branch is HEAD on" and "what sha
            # is it at" from the same process. Modeled here as a single
            # consistent pair rather than two independent answers, because
            # that consistency is the entire point of collapsing the two old
            # calls into one.
            self.pair_calls[key] = self.pair_calls.get(key, 0) + 1
            if "pair_output" in spec:
                out, rc = spec["pair_output"], spec.get("pair_rc", 0)
            else:
                target_ref = cmd.rsplit("refs/heads/", 1)[-1].strip("'\"")
                head_sha = spec.get("head", "headsha")
                checked_out = spec.get("head_ref", f"refs/heads/{target_ref}")
                branch_sha = (
                    head_sha if checked_out == f"refs/heads/{target_ref}"
                    else spec.get("other_branch_sha", "otherbranchsha")
                )
                out, rc = f"{head_sha}\n{branch_sha}", 0
        elif "rev-parse HEAD" in cmd:
            # Bare `rev-parse HEAD`, no branch argument: only a *reverted*,
            # pre-fix `_inspect_repo` ever issues this (see `stale_bare_head`
            # above) — current code always names the branch in the same call.
            out, rc = spec.get("stale_bare_head", spec.get("head", "headsha")), 0
        elif "merge-base --is-ancestor" in cmd:
            tip = cmd.split()[-2].strip("'")
            out, rc = "", 0 if tip in spec.get("contains", []) else 1
        elif cmd.startswith("git push"):
            self.pushes.append((cmd, Path(cwd)))
            out, rc, err = "", self._push_rc, self._push_err
        else:
            out, rc = "", 0

        class R:
            stdout = out
            stderr = err
            returncode = rc
        return R()


def _repo(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clean(**over):
    """A repo that is clean and exactly matches origin."""
    return {"status": "", "origin": "headsha", "head": "headsha", **over}


# --- what must be TRANSFERRED, not refused ----------------------------------
#
# These five were refusals under PR #419, for a reason that no longer holds:
# inventing a commit out of someone's work in progress was not a decision the
# tool could make, because that commit would have gone to ORIGIN. It now goes
# only to the operator's own run host, on a ref nothing else writes, leaving
# their repository untouched — so the same repo shapes route instead of stopping.

def test_tracked_changes_are_transferred_not_refused(tmp_path):
    """The whole point of the spec: run what the operator is editing."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status=" M src/app.py\n")})

    pre = inspect(FakeTask({"api": api}), shell)

    assert pre.ok
    assert [s.repo for s in pre.dirty] == ["api"]
    assert pre.to_push == []            # ac3: origin is not in this path
    assert shell.pushes == []


def test_staged_but_uncommitted_is_also_transferred(tmp_path):
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status="M  src/app.py\n")})
    pre = inspect(FakeTask({"api": api}), shell)
    assert pre.ok and [s.repo for s in pre.dirty] == ["api"]


def test_every_dirty_repo_is_transferred_not_just_the_first(tmp_path):
    """A multi-repo task must send every repo's tree; sending one and leaving
    the others on origin's revision is the stale-code failure again."""
    api, web = _repo(tmp_path, "api"), _repo(tmp_path, "web")
    shell = FakeShell({
        "api": _clean(status=" M a.py\n"),
        "web": _clean(status=" M b.ts\n"),
    })
    pre = inspect(FakeTask({"api": api, "web": web}), shell)
    assert sorted(s.repo for s in pre.dirty) == ["api", "web"]
    assert pre.ok and shell.pushes == []


def test_untracked_only_counts_as_dirty_and_travels(tmp_path):
    """ac9/ac11. Under #419 this only warned, because untracked files could not
    change what a push to origin carried. They are part of what the operator
    sees, and they now travel only between the operator's own two machines — so
    ANY porcelain output at all is dirty, or untracked files would never travel
    and ac11 would be unsatisfiable."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status="?? scratch.txt\n")})
    pre = inspect(FakeTask({"api": api}), shell)
    assert pre.ok
    assert [s.repo for s in pre.dirty] == ["api"]
    assert pre.to_push == []


def test_untracked_alongside_tracked_is_transferred_once(tmp_path):
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status="?? new.py\n M old.py\n")})
    pre = inspect(FakeTask({"api": api}), shell)
    assert pre.ok and [s.repo for s in pre.dirty] == ["api"]


# --- what must be refused ---------------------------------------------------

def test_a_failed_git_status_blocks_rather_than_reading_as_clean(tmp_path):
    """A broken repo's `git status` exits non-zero with EMPTY stdout, which is
    byte-identical to a clean tree. Trusting stdout alone dispatches a run over a
    repo whose state was never established — the exact thing this module exists
    to refuse."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(
        status="", status_rc=128,
        status_err="fatal: not a git repository (or any parent up to mount point /)\n",
    )})
    pre = inspect(FakeTask({"api": api}), shell)

    assert not pre.ok
    assert [s.blocked_reason for s in pre.blocked] == [UNREADABLE]
    assert shell.pushes == []

    msg = blocked_message(pre)
    assert "unreadable git state in api" in msg
    assert "not a git repository" in msg             # git's own words, verbatim
    assert "git status" in msg                       # the remedy: fix the repo
    assert "mship commit" not in msg                 # NOT the dirty-tree remedy


def test_a_missing_worktree_blocks_rather_than_being_skipped(tmp_path):
    """Silently dropping an uninspectable repo from the check still dispatches
    it: the run host materializes that repo's branch from origin regardless, so
    it would run an older pushed revision (or fail to materialize at all)."""
    api = _repo(tmp_path, "api")
    task = FakeTask({"api": api, "gone": tmp_path / "not-there"})
    shell = FakeShell({"api": _clean()})

    pre = inspect(task, shell)
    assert not pre.ok
    assert [s.repo for s in pre.blocked] == ["gone"]
    assert [s.blocked_reason for s in pre.blocked] == [MISSING_WORKTREE]
    assert [s.repo for s in pre.states] == ["api", "gone"]   # not dropped

    msg = blocked_message(pre)
    assert "missing worktree in gone" in msg
    assert "not-there does not exist" in msg
    assert "mship worktrees" in msg


def test_a_selected_repo_outside_the_tasks_worktrees_is_refused_not_skipped(tmp_path):
    """`--repos web` naming a repo that is not one of this task's repos at all
    (no entry in `task.worktrees`, unlike `test_a_missing_worktree_blocks_...`
    above where the entry exists but the path is gone) must not vanish from the
    check silently. `inspect` only ever iterates `task.worktrees`, so a `web`
    with no entry there would produce no RepoState and no refusal — while the
    run host materializes `feat/x` for `web` regardless."""
    api = _repo(tmp_path, "api")
    task = FakeTask({"api": api})
    shell = FakeShell({"api": _clean()})

    pre = inspect(task, shell, repos=["api", "web"])
    assert not pre.ok
    assert [s.repo for s in pre.blocked] == ["web"]
    assert [s.blocked_reason for s in pre.blocked] == [MISSING_WORKTREE]
    assert shell.touched == {"api"}                  # web was never even looked at
    assert shell.pushes == []

    msg = blocked_message(pre)
    assert "missing worktree in web" in msg
    assert "mship worktrees" in msg


def test_origin_ahead_of_head_is_refused_not_pushed(tmp_path):
    """A push cannot fast-forward from behind, so attempting one would replace
    the real remedy with a confusing git error."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(origin="0123456789abcdef", head="oldsha",
                                     contains=[])})
    pre = inspect(FakeTask({"api": api}), shell)

    assert not pre.ok
    assert [s.blocked_reason for s in pre.blocked] == [BEHIND_ORIGIN]
    assert pre.to_push == []
    assert shell.pushes == []

    msg = blocked_message(pre)
    assert "unpulled commits on origin in api" in msg
    assert "0123456789ab" in msg                     # which commit, exactly
    assert "pull --ff-only origin feat/x" in msg     # the remedy is pull, not push
    assert "fast-forward from behind is impossible" in msg


def test_a_worktree_on_another_branch_is_refused(tmp_path):
    """Every verdict here is reached by reading HEAD, and `push` publishes what
    those verdicts cleared. If HEAD is not the task's branch, the run host
    materializes one commit while a different one was inspected."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(head_ref="refs/heads/main")})
    pre = inspect(FakeTask({"api": api}), shell)

    assert not pre.ok
    assert [s.blocked_reason for s in pre.blocked] == [WRONG_BRANCH]
    assert pre.to_push == []
    assert shell.pushes == []

    msg = blocked_message(pre)
    assert "worktree is not on the task's branch in api" in msg
    assert "HEAD is main, not feat/x" in msg           # which branch, exactly
    assert "checkout feat/x" in msg                    # the remedy
    assert "mship commit" not in msg                   # not a dirty-tree problem


def test_a_checkout_between_branch_check_and_sha_capture_cannot_tear_the_pair(tmp_path):
    """The ninth bypass, one level deeper than the eighth: branch identity and
    the sha it certifies must come from ONE read of the repo's refs, not two.
    The OLD shape was `symbolic-ref` (verify the branch) THEN `rev-parse HEAD`
    (capture the sha) — a `git checkout` landing in the window between those
    two calls would verify branch A and capture a sha that actually belongs to
    branch B, and nothing downstream could tell: it just carries on with a
    `head_sha` that was never on the branch preflight thinks it inspected.

    Made deterministic by giving the fake shell two DIFFERENT answers for the
    two different call shapes this race is about: `head_ref`/`head` describe
    what the SINGLE atomic `rev-parse HEAD refs/heads/<branch>` call sees (a
    consistent pair — HEAD really is on the task's branch, at `head_sha`), and
    `stale_bare_head` describes what a SEPARATE, later, argument-less
    `rev-parse HEAD` would see if a checkout to another branch landed in
    between — the exact torn read that is only reachable by an implementation
    that still makes two calls.

    Current code never issues that second, bare call at all (see `FakeShell`
    docstring), so this passes by construction: `pair_calls` is exactly 1 and
    the sha carried forward is the one the atomic read certified, not the
    racy one. Stashing the production fix and rerunning this test reintroduces
    the old two-call shape, which DOES reach `stale_bare_head` and fails this
    exact assertion — see the task's red-before-green note.
    """
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(
        head_ref="refs/heads/feat/x",   # the atomic read: HEAD is on feat/x
        head="correct",                  # ...at this sha
        stale_bare_head="raced",         # a checkout-then-bare-read would see this
        origin=None,                      # keep the origin comparison out of the way
    )})

    pre = inspect(FakeTask({"api": api}), shell)

    assert pre.ok
    assert pre.states[0].head_sha == "correct"   # the atomically-verified sha
    assert shell.pair_calls == {"api": 1}        # never re-read -- nothing to race


def test_a_torn_pair_within_the_single_rev_parse_is_refused_not_pinned(tmp_path):
    """The decisive case for the self-verifying claim: `git rev-parse HEAD
    refs/heads/<branch>` still resolves its two arguments SEQUENTIALLY inside
    one process, so a window survives WITHIN that single command even though
    the two old separate commands are gone. This is not the same bypass as
    `test_a_checkout_between_branch_check_and_sha_capture_cannot_tear_the_pair`
    above (that one shows there is no longer a SECOND call to race against);
    this one shows that a mutation landing DURING the one remaining call
    cannot slip a wrong sha through either.

    Reasoning through it: whatever lands between the two reads either leaves
    them agreeing — in which case the pinned sha genuinely is a value both
    reads observed, i.e. really was the branch's tip at some consistent
    instant — or makes them disagree, which `_inspect_repo` already refuses
    as WRONG_BRANCH. There is no third outcome where they agree on a sha that
    is not actually the branch's tip. A force-checkout that ALSO resets the
    branch (`git checkout -B <branch> <other-sha>` — a subagent restarting the
    branch from a different base is the realistic trigger) is exactly the
    shape that would produce this: HEAD's read observes the tip from before
    the reset, the direct `refs/heads/<branch>` read observes it after.

    Modeled with `pair_output` so the two lines are asserted directly, rather
    than through `head`/`head_ref` (which can only express "really on another
    branch", already covered above) — this is a same-branch race, not a
    different-branch worktree.
    """
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(
        pair_output="before-reset-sha\nafter-reset-sha\n",
        origin=None,
    )})
    pre = inspect(FakeTask({"api": api}), shell)

    assert not pre.ok
    assert [s.blocked_reason for s in pre.blocked] == [WRONG_BRANCH]
    assert pre.states[0].head_sha is None       # never pinned, torn or not
    assert shell.pushes == []


def test_a_commit_landing_between_the_two_reads_is_refused_not_pinned(tmp_path):
    """Same guard, different realistic trigger: no checkout at all, just
    another process (a second worktree of the same repo, sharing refs)
    committing to the task's branch while this `rev-parse` is mid-flight.
    HEAD's read observes the pre-commit tip; the direct branch-ref read,
    running microseconds later in the same process, observes the new one."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(
        pair_output="pre-commit-sha\npost-commit-sha\n",
        origin=None,
    )})
    pre = inspect(FakeTask({"api": api}), shell)

    assert not pre.ok
    assert [s.blocked_reason for s in pre.blocked] == [WRONG_BRANCH]
    assert pre.states[0].head_sha is None


def test_one_ref_failing_to_resolve_mid_command_is_refused_not_pinned(tmp_path):
    """The branch ref vanishing (deleted, or renamed out from under git) between
    the two reads is a torn pair too, just an asymmetric one: git emits a
    resolved line for the argument it could still answer and none for the one
    that broke mid-command, so the second line is simply missing rather than
    present-but-different. `_inspect_repo` must not read the missing second
    line as "no branch given, HEAD alone is enough" — an empty `branch_tip`
    already fails `head_sha != branch_tip` because a real sha is never
    empty, but this pins that down explicitly rather than leaving it as a
    side effect of the disagreeing-pair cases above."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(
        pair_output="only-head-resolved\n",   # branch ref line never arrives
        pair_rc=128,                          # git exits non-zero: one arg failed
        origin=None,
    )})
    pre = inspect(FakeTask({"api": api}), shell)

    assert not pre.ok
    assert [s.blocked_reason for s in pre.blocked] == [WRONG_BRANCH]
    assert pre.states[0].head_sha is None


def test_a_detached_worktree_at_the_task_tip_is_refused_before_origin(tmp_path):
    """SHA equality cannot substitute for being attached to the task branch."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(
        head_ref="",
        pair_output="headsha\nheadsha\n",
    )})
    pre = inspect(FakeTask({"api": api}), shell)

    assert not pre.ok
    assert [s.blocked_reason for s in pre.blocked] == [WRONG_BRANCH]
    assert "HEAD is detached, not feat/x" in blocked_message(pre)
    assert not any("ls-remote" in command for command in shell.commands)
    assert shell.pushes == []


def test_a_detached_worktree_is_refused_and_says_so(tmp_path):
    """Detached is the same finding with no branch name to report: `symbolic-ref
    --quiet` exits non-zero with empty output, which must not read as a match."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(head_ref="")})
    pre = inspect(FakeTask({"api": api}), shell)

    assert [s.blocked_reason for s in pre.blocked] == [WRONG_BRANCH]
    assert "HEAD is detached, not feat/x" in blocked_message(pre)


def test_the_branch_is_checked_before_the_tree_is_judged_dirty(tmp_path):
    """A wrong-branch worktree that is also dirty must report the branch. Under
    #419 the alternative was telling the operator to `mship commit` onto the
    wrong branch; now it is silently SENDING that branch's tree as if it were
    the task's, which is worse still. So WRONG_BRANCH keeps winning."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status=" M a.py\n", head_ref="refs/heads/main")})
    pre = inspect(FakeTask({"api": api}), shell)
    assert [s.blocked_reason for s in pre.blocked] == [WRONG_BRANCH]
    assert pre.dirty == []


def test_a_path_that_is_not_a_git_repo_is_unreadable_not_wrong_branch(tmp_path):
    """Ordering guard: `git status` failing means nothing about HEAD can be
    asked, so the remedy is "fix the repo", not "check out the branch"."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(
        status="", status_rc=128, status_err="fatal: not a git repository\n",
        head_ref="",
    )})
    assert [s.blocked_reason for s in inspect(FakeTask({"api": api}), shell).blocked] \
        == [UNREADABLE]


def test_an_unreachable_origin_blocks(tmp_path):
    """Origin is the only authority on what the run host checks out; if it will
    not answer, nothing was verified."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(
        ls_remote_rc=128, ls_remote_err="fatal: could not read from remote repository\n",
    )})
    pre = inspect(FakeTask({"api": api}), shell)

    assert not pre.ok
    assert [s.blocked_reason for s in pre.blocked] == [ORIGIN_UNREACHABLE]
    assert "could not read from remote repository" in blocked_message(pre)


def test_each_blocked_reason_gets_its_own_section(tmp_path):
    """A conflicted repo and a stale one need BOTH remedies, not whichever was
    found first — the fix for one is actively wrong for the other."""
    api, web = _repo(tmp_path, "api"), _repo(tmp_path, "web")
    (api / ".git").mkdir(parents=True, exist_ok=True)
    (api / ".git" / "MERGE_HEAD").write_text("abc\n")
    shell = FakeShell({
        "api": _clean(status="UU a.py\n"),
        "web": _clean(origin="beefbeefbeef", head="oldsha", contains=[]),
    })
    msg = blocked_message(inspect(FakeTask({"api": api, "web": web}), shell))
    assert "merge or rebase in progress in api" in msg
    assert "unpulled commits on origin in web" in msg
    assert "--abort" in msg and "pull --ff-only" in msg


def test_a_conflicted_repo_is_refused(tmp_path):
    """ac12: the working tree IS what gets sent, and mid-conflict it holds
    conflict markers rather than code anyone meant to run."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status="UU src/app.py\n")})

    pre = inspect(FakeTask({"api": api}), shell)

    assert not pre.ok
    assert [s.blocked_reason for s in pre.blocked] == [IN_PROGRESS]
    assert pre.dirty == [] and shell.pushes == []

    msg = blocked_message(pre)
    assert "merge or rebase in progress in api" in msg
    assert "src/app.py" in msg          # which file, exactly
    assert f'git -C "{api}" status' in msg
    assert f'git -C "{api}" rebase --abort' not in msg


def test_a_mid_rebase_repo_is_refused_ahead_of_the_branch_check(tmp_path):
    """Ordering matters, and it is not cosmetic: `git rebase` DETACHES HEAD, so
    checking the branch first would refuse this as WRONG_BRANCH and print
    `git checkout <branch>` — a command that abandons the rebase. The
    in-progress check is deliberately ordered ahead of it."""
    api = _repo(tmp_path, "api")
    (api / ".git" / "rebase-merge").mkdir(parents=True)
    shell = FakeShell({"api": _clean(status="", head_ref="")})   # detached

    pre = inspect(FakeTask({"api": api}), shell)

    assert [s.blocked_reason for s in pre.blocked] == [IN_PROGRESS]
    msg = blocked_message(pre)
    assert "rebase" in msg
    assert f'continue: git -C "{api}" rebase --continue' in msg
    assert f'abort: git -C "{api}" rebase --abort' in msg
    assert "checkout" not in msg        # NOT the wrong-branch remedy


def test_a_mid_merge_repo_is_refused_even_with_a_clean_tree(tmp_path):
    """`git merge --no-commit` leaves MERGE_HEAD with nothing unmerged: the
    porcelain alone cannot see it, which is why the git dir is consulted."""
    api = _repo(tmp_path, "api")
    (api / ".git").mkdir(parents=True, exist_ok=True)
    (api / ".git" / "MERGE_HEAD").write_text("abc\n")
    shell = FakeShell({"api": _clean(status="")})
    pre = inspect(FakeTask({"api": api}), shell)

    assert [s.blocked_reason for s in pre.blocked] == [IN_PROGRESS]
    msg = blocked_message(pre)
    assert f'continue: git -C "{api}" merge --continue' in msg
    assert f'abort: git -C "{api}" merge --abort' in msg

    assert f'git -C "{api}" rebase --abort' not in msg


def test_a_cherry_pick_in_progress_is_refused(tmp_path):
    api = _repo(tmp_path, "api")
    (api / ".git").mkdir(parents=True, exist_ok=True)
    (api / ".git" / "CHERRY_PICK_HEAD").write_text("abc\n")
    shell = FakeShell({"api": _clean(status="")})
    pre = inspect(FakeTask({"api": api}), shell)

    assert [s.blocked_reason for s in pre.blocked] == [IN_PROGRESS]
    msg = blocked_message(pre)
    assert f'continue: git -C "{api}" cherry-pick --continue' in msg
    assert f'abort: git -C "{api}" cherry-pick --abort' in msg

    assert f'git -C "{api}" rebase --abort' not in msg


def test_a_mid_rebase_apply_repo_lists_rebase_and_git_am_recovery_separately(tmp_path):
    api = _repo(tmp_path, "api")
    (api / ".git" / "rebase-apply").mkdir(parents=True)
    shell = FakeShell({"api": _clean(status="", head_ref="")})

    pre = inspect(FakeTask({"api": api}), shell)

    assert [s.blocked_reason for s in pre.blocked] == [IN_PROGRESS]
    msg = blocked_message(pre)
    assert f'continue: git -C "{api}" rebase --continue' in msg
    assert f'continue: git -C "{api}" am --continue' in msg
    assert f'abort: git -C "{api}" rebase --abort' in msg
    assert f'abort: git -C "{api}" am --abort' in msg
    assert "# or git am" not in msg


def test_clean_sequencer_is_refused_before_wrong_branch_with_both_recoveries(tmp_path):
    api = _repo(tmp_path, "api")
    (api / ".git" / "sequencer").mkdir(parents=True)
    shell = FakeShell({"api": _clean(status="", head_ref="refs/heads/other")})

    pre = inspect(FakeTask({"api": api}), shell)

    assert [s.blocked_reason for s in pre.blocked] == [IN_PROGRESS]
    msg = blocked_message(pre)
    for command in (
        "cherry-pick --continue",
        "revert --continue",
        "cherry-pick --abort",
        "revert --abort",
    ):
        assert f'git -C "{api}" {command}' in msg
    assert "checkout" not in msg
    assert f'git -C "{api}" rebase --abort' not in msg


def test_bisect_is_refused_before_wrong_branch_with_term_agnostic_recovery(tmp_path):
    api = _repo(tmp_path, "api")
    (api / ".git").mkdir(parents=True, exist_ok=True)
    (api / ".git" / "BISECT_START").write_text("start\n")
    shell = FakeShell({"api": _clean(status="", head_ref="refs/heads/other")})

    pre = inspect(FakeTask({"api": api}), shell)

    assert [s.blocked_reason for s in pre.blocked] == [IN_PROGRESS]
    msg = blocked_message(pre)
    for command in ("bisect skip", "bisect reset"):
        assert f'git -C "{api}" {command}' in msg
    assert f'git -C "{api}" bisect good' not in msg
    assert f'git -C "{api}" bisect bad' not in msg
    assert "checkout" not in msg
    assert f'git -C "{api}" rebase --abort' not in msg


def test_bisect_start_is_active_only_when_nonempty(tmp_path):
    api = _repo(tmp_path, "api")
    (api / ".git").mkdir(parents=True, exist_ok=True)
    shell = FakeShell({"api": _clean(status="")})

    (api / ".git" / "BISECT_START").touch()
    empty = inspect(FakeTask({"api": api}), shell)

    (api / ".git" / "BISECT_START").write_text("start\n")
    active = inspect(FakeTask({"api": api}), shell)

    assert empty.ok
    assert empty.blocked == []
    assert [s.blocked_reason for s in active.blocked] == [IN_PROGRESS]


def test_bisect_recovery_commands_remain_copyable_with_unmerged_paths(tmp_path):
    api = _repo(tmp_path, "api")
    (api / ".git").mkdir(parents=True, exist_ok=True)
    (api / ".git" / "BISECT_START").write_text("start\n")
    shell = FakeShell({"api": _clean(status="UU src/app.py\n")})

    lines = blocked_message(inspect(FakeTask({"api": api}), shell)).splitlines()
    skip = f'continue: git -C "{api}" bisect skip'
    reset = f'abort: git -C "{api}" bisect reset'

    assert skip in lines
    assert reset in lines
    assert lines[lines.index(reset) + 1] == "unresolved: src/app.py"


def test_historical_bisect_log_without_active_state_is_not_refused(tmp_path):
    api = _repo(tmp_path, "api")
    (api / ".git").mkdir(parents=True, exist_ok=True)
    (api / ".git" / "BISECT_LOG").write_text("git bisect start --term-old=stable --term-new=broken\n")
    shell = FakeShell({"api": _clean(status="")})

    pre = inspect(FakeTask({"api": api}), shell)

    assert pre.ok
    assert pre.blocked == []


def test_a_revert_in_progress_does_not_offer_a_rebase_abort(tmp_path):
    api = _repo(tmp_path, "api")
    (api / ".git").mkdir(parents=True, exist_ok=True)
    (api / ".git" / "REVERT_HEAD").write_text("abc\n")
    shell = FakeShell({"api": _clean(status="")})

    msg = blocked_message(inspect(FakeTask({"api": api}), shell))

    assert f'git -C "{api}" revert --continue' in msg
    assert f'git -C "{api}" revert --abort' in msg
    assert f'git -C "{api}" rebase --abort' not in msg




def test_an_unanswerable_git_dir_is_unreadable_not_transferred(tmp_path):
    """Same rule as fix 1's `git status` guard: a repo whose state could not be
    established is refused, never assumed clean — and never shipped."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status=" M a.py\n", git_dir="")})
    pre = inspect(FakeTask({"api": api}), shell)
    assert [s.blocked_reason for s in pre.blocked] == [UNREADABLE]
    assert pre.dirty == []


# --- what must be pushed ----------------------------------------------------

def test_a_clean_branch_missing_from_origin_is_pushed(tmp_path):
    """The case that fails outright today: nothing pushes before `mship finish`."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(origin=None)})
    pre = inspect(FakeTask({"api": api}), shell)
    assert pre.ok and [s.push_reason for s in pre.to_push] == ["not on origin"]

    pushed, err = push(pre, shell)
    assert err is None and pushed == ["api"]
    # The exact sha that was INSPECTED — see `test_a_detached_worktree_never_
    # publishes_a_commit_that_was_not_inspected` for why the branch name alone is
    # not it, and `test_a_commit_landing_between_inspect_and_push_is_not_what_
    # gets_pushed` for why `HEAD` re-resolved at push time is not either.
    assert shell.pushes
    assert "push -u origin headsha:refs/heads/feat/x" in shell.pushes[0][0]


def test_a_clean_branch_ahead_of_origin_is_pushed(tmp_path):
    """The dangerous-silent case: origin has an older commit."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(origin="oldsha", head="newsha",
                                     contains=["oldsha"])})
    pre = inspect(FakeTask({"api": api}), shell)
    assert [s.push_reason for s in pre.to_push] == ["ahead of origin"]
    assert push(pre, shell)[1] is None


def test_an_up_to_date_repo_costs_no_push(tmp_path):
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean()})
    pre = inspect(FakeTask({"api": api}), shell)
    assert pre.to_push == []
    assert push(pre, shell) == ([], None)
    assert shell.pushes == []


def test_every_affected_repo_is_pushed(tmp_path):
    """A task has a branch per repo and the run host materializes each
    separately, so pushing one leaves the others stale."""
    api, web = _repo(tmp_path, "api"), _repo(tmp_path, "web")
    shell = FakeShell({
        "api": _clean(origin=None),
        "web": _clean(origin="oldsha", head="newsha", contains=["oldsha"]),
    })
    pre = inspect(FakeTask({"api": api, "web": web}), shell)
    pushed, err = push(pre, shell)
    assert err is None and sorted(pushed) == ["api", "web"]
    assert len(shell.pushes) == 2


def test_a_failed_push_stops_the_run(tmp_path):
    """Proceeding after a failed push is the exact failure being prevented."""
    api = _repo(tmp_path, "api")
    shell = FakeShell(
        {"api": _clean(origin=None)},
        push_rc=1, push_err="remote: permission denied\n",
    )
    pre = inspect(FakeTask({"api": api}), shell)
    pushed, err = push(pre, shell)
    assert pushed == [] and err is not None
    assert "could not push api" in err and "permission denied" in err


# --- scoping to the repos actually dispatched -------------------------------

def test_repos_scoping_ignores_a_dirty_repo_the_run_never_touches(tmp_path):
    """`--repos api` dispatches only api; work in progress in web is neither a
    reason to stop nor a tree to ship."""
    api, web = _repo(tmp_path, "api"), _repo(tmp_path, "web")
    shell = FakeShell({
        "api": _clean(),
        "web": _clean(status=" M b.ts\n"),
    })
    pre = inspect(FakeTask({"api": api, "web": web}), shell, repos=["api"])
    assert pre.ok
    assert [s.repo for s in pre.states] == ["api"]
    assert pre.dirty == []


def test_repos_scoping_does_not_push_an_unselected_repo(tmp_path):
    """A narrowly scoped command must not push a repo the operator never named.
    web's branch is not on origin, so an unscoped preflight would push it."""
    api, web = _repo(tmp_path, "api"), _repo(tmp_path, "web")
    shell = FakeShell({"api": _clean(), "web": _clean(origin=None)})
    pre = inspect(FakeTask({"api": api, "web": web}), shell, repos=["api"])
    pushed, err = push(pre, shell)
    assert err is None and pushed == []
    assert shell.pushes == []
    assert shell.touched == {"api"}          # never even inspected


def test_no_scope_means_every_repo_the_task_touches(tmp_path):
    api, web = _repo(tmp_path, "api"), _repo(tmp_path, "web")
    shell = FakeShell({"api": _clean(), "web": _clean()})
    pre = inspect(FakeTask({"api": api, "web": web}), shell)
    assert [s.repo for s in pre.states] == ["api", "web"]


# --- how a dirty repo is routed ---------------------------------------------

def test_a_dirty_repo_never_asks_origin(tmp_path):
    """BEHIND_ORIGIN and ORIGIN_UNREACHABLE exist because the run host
    materializes a BRANCH from origin. On this path it materializes a scratch
    ref this machine pushes, with no fetch at all, so origin's answer cannot
    change what executes — and a round trip that cannot change the outcome is a
    round trip not worth taking. Both refusals keep their full force on the
    clean path (see the tests above)."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(
        status=" M a.py\n",
        origin="0123456789abcdef", head="oldsha", contains=[],   # would be BEHIND_ORIGIN
    )})

    pre = inspect(FakeTask({"api": api}), shell)

    assert pre.ok
    assert [s.repo for s in pre.dirty] == ["api"]
    assert pre.blocked == []                                # not BEHIND_ORIGIN
    assert not any("ls-remote" in c for c in shell.commands)
    assert not any("merge-base" in c for c in shell.commands)


def test_a_dirty_repo_carries_the_sha_inspect_certified(tmp_path):
    """Fix 9, carried onto the new path: `synthesize_commit` parents the
    snapshot on THIS sha rather than re-resolving HEAD moments later."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status=" M a.py\n", head="certified")})
    pre = inspect(FakeTask({"api": api}), shell)
    assert [s.head_sha for s in pre.dirty] == ["certified"]


def test_a_git_root_child_and_its_parent_dedupe_to_one_transfer(tmp_path):
    """ac7: one git repository, one scratch ref. The child's tree IS the
    parent's, so pushing both would send the same objects twice under two names
    — and the run host resolves the child under the materialized parent anyway.
    The parent is the survivor because that is the name the host materializes."""
    mono = _repo(tmp_path, "mono")
    child = _repo(tmp_path / "mono", "pkg")
    shell = FakeShell({
        "mono": _clean(status=" M a.py\n"),
        "pkg": _clean(status=" M a.py\n"),
    })
    config = WorkspaceConfig(workspace="t", repos={
        "mono": RepoConfig(path=mono, type="service"),
        "pkg": RepoConfig(path=Path("pkg"), type="service", git_root="mono"),
    })

    pre = inspect(FakeTask({"mono": mono, "pkg": child}), shell, config=config)

    assert [s.git_repo for s in pre.dirty] == ["mono"]
    assert [s.repo for s in pre.dirty] == ["mono"]
    assert [s.repo for s in pre.states] == ["mono", "pkg"]   # both still inspected


def test_without_a_config_every_repo_is_its_own_git_repo(tmp_path):
    """`config` is optional so the 25 untouched tests in this file keep calling
    `inspect(task, shell)`. Absent it there is nothing to collapse against."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status=" M a.py\n")})
    pre = inspect(FakeTask({"api": api}), shell)
    assert [s.git_repo for s in pre.dirty] == ["api"]


# --- against real git, where staleness is real ------------------------------

# The operator's own git config must not reach these repos: a global
# `commit.gpgsign`, `insteadOf`, or hooksPath would make the outcome depend on
# whose machine the suite runs on.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env=_GIT_ENV,
    ).stdout.strip()


class RealShell:
    """The narrow slice of `ShellRunner` the preflight uses, over real git."""

    def run(self, command, cwd=None, env=None, timeout=None):
        proc = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env={**_GIT_ENV, **(env or {})},
        )

        class R:
            stdout = proc.stdout
            stderr = proc.stderr
            returncode = proc.returncode
        return R()


def _real_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A worktree on `feat/x` with a real bare origin, one commit in, in sync."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main", ".")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "--initial-branch=main", ".")
    _git(work, "remote", "add", "origin", str(origin))
    (work / "f.txt").write_text("one\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "one")
    _git(work, "checkout", "-b", "feat/x")
    _git(work, "push", "-u", "origin", "feat/x")
    return origin, work


def test_real_repo_in_sync_needs_no_push(tmp_path):
    _, work = _real_repo(tmp_path)
    pre = inspect(FakeTask({"api": work}), RealShell())
    assert pre.ok and pre.to_push == []


def test_a_stale_remote_tracking_ref_does_not_hide_a_newer_origin(tmp_path):
    """THE finding. Another machine advances the branch on origin; this clone's
    `origin/feat/x` still points at the old commit, so `@{u}..HEAD` reports 0 and
    a local-only check concludes "up to date, nothing to push" — after which the
    run host fetches origin and executes a commit the operator has never seen.

    Note what is NOT done here: no `git fetch`. The local remote-tracking ref is
    left deliberately stale, which is the entire point.
    """
    origin, work = _real_repo(tmp_path)

    # A second clone stands in for the other machine, and pushes.
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "clone", str(origin), ".")
    _git(other, "checkout", "feat/x")
    (other / "f.txt").write_text("two\n")
    _git(other, "add", "f.txt")
    _git(other, "commit", "-m", "two")
    _git(other, "push", "origin", "feat/x")
    newer = _git(other, "rev-parse", "HEAD")

    # Proof the stale case is actually being exercised: the local check the
    # finding is about still says "nothing to do".
    assert _git(work, "rev-list", "--count", "@{u}..HEAD") == "0"
    assert _git(work, "rev-parse", "@{u}") != newer

    pre = inspect(FakeTask({"api": work}), RealShell())
    assert not pre.ok
    assert [s.blocked_reason for s in pre.blocked] == [BEHIND_ORIGIN]
    assert pre.to_push == []
    assert newer[:12] in blocked_message(pre)


def test_real_repo_ahead_of_origin_is_pushed_and_lands(tmp_path):
    origin, work = _real_repo(tmp_path)
    (work / "f.txt").write_text("local\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "local")
    head = _git(work, "rev-parse", "HEAD")

    shell = RealShell()
    pre = inspect(FakeTask({"api": work}), shell)
    assert pre.ok and [s.push_reason for s in pre.to_push] == ["ahead of origin"]

    pushed, err = push(pre, shell)
    assert err is None and pushed == ["api"]
    assert _git(origin, "rev-parse", "refs/heads/feat/x") == head


def test_a_commit_landing_between_inspect_and_push_is_not_what_gets_pushed(tmp_path):
    """The local half of the eighth bypass: `push` used to re-resolve `HEAD` at
    push time via `HEAD:refs/heads/<branch>`. If anything commits in the SAME
    worktree between `inspect` and `push` — a subagent, a background job, this
    very workspace's own pattern of running commands while others are in flight —
    that re-resolution would publish the NEWER commit, one `inspect` never
    looked at, which is the exact silent-stale-code failure this module exists to
    prevent, just moved one step later.

    Capturing the sha during `inspect` (`RepoState.head_sha`) and pushing that
    exact sha instead of re-resolving `HEAD` closes it: whatever HEAD becomes
    afterward, the push still names the commit that was actually inspected. This
    does NOT cover a writer advancing the branch on ORIGIN after the push lands —
    that gap is a mutable ref on another machine and is documented, not fixed,
    in `docs/remote-run.md`.
    """
    origin, work = _real_repo(tmp_path)
    (work / "f.txt").write_text("inspected\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "inspected")
    inspected = _git(work, "rev-parse", "HEAD")

    shell = RealShell()
    pre = inspect(FakeTask({"api": work}), shell)
    assert pre.ok and [s.push_reason for s in pre.to_push] == ["ahead of origin"]
    assert [s.head_sha for s in pre.to_push] == [inspected]

    # The race: something else commits in the SAME worktree after inspection,
    # before `push` runs.
    (work / "f.txt").write_text("raced\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "raced after inspection")
    raced = _git(work, "rev-parse", "HEAD")
    assert raced != inspected

    pushed, err = push(pre, shell)
    assert err is None and pushed == ["api"]
    assert _git(origin, "rev-parse", "refs/heads/feat/x") == inspected
    assert _git(origin, "rev-parse", "refs/heads/feat/x") != raced


def test_real_repo_diverged_from_origin_is_refused(tmp_path):
    """Diverged is behind-shaped from the run host's point of view: origin's tip
    is not in HEAD's history, so the run would execute a commit the operator does
    not have. A push would be rejected as a non-fast-forward."""
    origin, work = _real_repo(tmp_path)

    other = tmp_path / "other"
    other.mkdir()
    _git(other, "clone", str(origin), ".")
    _git(other, "checkout", "feat/x")
    (other / "g.txt").write_text("theirs\n")
    _git(other, "add", "g.txt")
    _git(other, "commit", "-m", "theirs")
    _git(other, "push", "origin", "feat/x")

    (work / "h.txt").write_text("mine\n")
    _git(work, "add", "h.txt")
    _git(work, "commit", "-m", "mine")

    pre = inspect(FakeTask({"api": work}), RealShell())
    assert [s.blocked_reason for s in pre.blocked] == [BEHIND_ORIGIN]


def test_real_repo_branch_absent_from_origin_is_pushed(tmp_path):
    _, work = _real_repo(tmp_path)
    _git(work, "checkout", "-b", "feat/never-pushed")
    pre = inspect(FakeTask({"api": work}, branch="feat/never-pushed"), RealShell())
    assert [s.push_reason for s in pre.to_push] == ["not on origin"]


def test_a_detached_worktree_never_publishes_a_commit_that_was_not_inspected(tmp_path):
    """THE second finding, stated as the invariant it protects: *the commit that
    reaches origin is the commit preflight inspected*.

    A detached worktree splits the two. Every check reads HEAD, but pushing the
    BRANCH NAME (`git push -u origin feat/x`) resolves the stale local branch ref
    instead — git reports `Everything up-to-date`, exit 0, and origin keeps the
    old commit. The run host then resets to that old commit, and the preflight
    that cleared the run had looked at something else entirely.

    Real git, real bare origin: a mocked shell has no notion of `feat/x` and
    `HEAD` being two different commits, which is the whole point.
    """
    origin, work = _real_repo(tmp_path)
    _git(work, "checkout", "--detach", "HEAD")
    (work / "d.txt").write_text("detached\n")
    _git(work, "add", "d.txt")
    _git(work, "commit", "-m", "detached work")
    inspected = _git(work, "rev-parse", "HEAD")
    assert _git(work, "rev-parse", "feat/x") != inspected   # the split is real

    shell = RealShell()
    pre = inspect(FakeTask({"api": work}), shell)

    if pre.ok:
        push(pre, shell)
        assert _git(origin, "rev-parse", "refs/heads/feat/x") == inspected
    else:
        assert [s.blocked_reason for s in pre.blocked] == [WRONG_BRANCH]
        assert pre.to_push == []


def test_a_real_worktree_on_another_branch_is_refused_and_origin_is_untouched(tmp_path):
    """The everyday shape of the same thing: someone checked another branch out
    inside the task worktree. Silently pushing its HEAD would republish the
    TASK's branch on origin as that other branch's commit — a shared ref moved to
    something the operator never named."""
    origin, work = _real_repo(tmp_path)
    before = _git(origin, "rev-parse", "refs/heads/feat/x")
    _git(work, "checkout", "-b", "side")
    (work / "s.txt").write_text("side\n")
    _git(work, "add", "s.txt")
    _git(work, "commit", "-m", "side")

    shell = RealShell()
    pre = inspect(FakeTask({"api": work}), shell)      # task branch is feat/x
    assert [s.blocked_reason for s in pre.blocked] == [WRONG_BRANCH]

    push(pre, shell)                                    # nothing to push
    assert _git(origin, "rev-parse", "refs/heads/feat/x") == before
    assert "HEAD is side, not feat/x" in blocked_message(pre)


def test_real_repo_that_is_not_a_git_worktree_blocks(tmp_path):
    """`git status` exits non-zero with empty stdout here — the shape that read
    as a clean tree."""
    plain = tmp_path / "plain"
    plain.mkdir()
    pre = inspect(FakeTask({"api": plain}), RealShell())
    assert [s.blocked_reason for s in pre.blocked] == [UNREADABLE]


def test_a_real_dirty_repo_is_transferred_and_origin_is_untouched(tmp_path):
    """ac3 against real git: the whole point is that origin sees nothing."""
    origin, work = _real_repo(tmp_path)
    before = _git(origin, "rev-parse", "refs/heads/feat/x")
    (work / "f.txt").write_text("uncommitted\n")
    (work / "scratch.txt").write_text("untracked\n")

    shell = RealShell()
    pre = inspect(FakeTask({"api": work}), shell)

    assert pre.ok
    assert [s.repo for s in pre.dirty] == ["api"]
    assert pre.to_push == []
    assert push(pre, shell) == ([], None)
    assert _git(origin, "rev-parse", "refs/heads/feat/x") == before


def test_a_real_conflicted_repo_is_refused(tmp_path):
    """ac12 against real git: a real merge conflict, real MERGE_HEAD, real
    `UU` porcelain — the exact state a mock can only assert about."""
    _, work = _real_repo(tmp_path)
    _git(work, "checkout", "-b", "side")
    (work / "f.txt").write_text("side\n")
    _git(work, "commit", "-am", "side")
    _git(work, "checkout", "feat/x")
    (work / "f.txt").write_text("mine\n")
    _git(work, "commit", "-am", "mine")
    subprocess.run(["git", "merge", "side"], cwd=work, capture_output=True, env=_GIT_ENV)

    pre = inspect(FakeTask({"api": work}), RealShell())

    assert [s.blocked_reason for s in pre.blocked] == [IN_PROGRESS]
    assert pre.dirty == []
    assert "--abort" in blocked_message(pre)
