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

from mship.core.remote_preflight import (
    BEHIND_ORIGIN,
    DIRTY,
    MISSING_WORKTREE,
    ORIGIN_UNREACHABLE,
    UNREADABLE,
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
    not have it); `head` is this worktree's HEAD; `contains` lists the shas that
    are ancestors of HEAD, which is how `merge-base --is-ancestor` is answered.
    """

    def __init__(self, answers, push_rc=0, push_err=""):
        self._answers = answers
        self._push_rc = push_rc
        self._push_err = push_err
        self.pushes: list[tuple[str, Path]] = []
        self.touched: set[str] = set()

    def run(self, cmd, cwd=None, **kw):
        key = Path(cwd).name
        spec = self._answers[key]
        self.touched.add(key)
        err = ""
        if "status --porcelain" in cmd:
            out, rc = spec["status"], spec.get("status_rc", 0)
            err = spec.get("status_err", "")
        elif "ls-remote" in cmd:
            if spec.get("ls_remote_rc"):
                out, rc, err = "", spec["ls_remote_rc"], spec.get("ls_remote_err", "")
            elif spec.get("origin") is None:
                out, rc = "", 0
            else:
                out, rc = f"{spec['origin']}\trefs/heads/feat/x\n", 0
        elif "rev-parse HEAD" in cmd:
            out, rc = spec.get("head", "headsha"), 0
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


# --- what must be refused ---------------------------------------------------

def test_tracked_changes_block_the_run(tmp_path):
    """The dangerous case: the remote would run the last pushed revision."""
    api = _repo(tmp_path, "api")
    task = FakeTask({"api": api})
    shell = FakeShell({"api": _clean(status=" M src/app.py\n")})

    pre = inspect(task, shell)
    assert not pre.ok
    assert [s.repo for s in pre.blocked] == ["api"]
    assert shell.pushes == []                       # nothing pushed while blocked

    msg = blocked_message(pre)
    assert "uncommitted changes in api" in msg
    assert "last PUSHED revision" in msg
    assert "mship commit" in msg                    # multi-repo commit named first
    assert str(api) in msg                          # exact per-repo push command


def test_staged_but_uncommitted_also_blocks(tmp_path):
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status="M  src/app.py\n")})
    assert not inspect(FakeTask({"api": api}), shell).ok


def test_every_dirty_repo_is_named_not_just_the_first(tmp_path):
    """A multi-repo task must not report one repo and leave the others to
    surface one at a time."""
    api, web = _repo(tmp_path, "api"), _repo(tmp_path, "web")
    shell = FakeShell({
        "api": _clean(status=" M a.py\n"),
        "web": _clean(status=" M b.ts\n"),
    })
    pre = inspect(FakeTask({"api": api, "web": web}), shell)
    assert sorted(s.repo for s in pre.blocked) == ["api", "web"]
    assert "api, web" in blocked_message(pre)


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
    """A dirty repo and a stale one need BOTH remedies, not whichever was found
    first — the fix for one is actively wrong for the other."""
    api, web = _repo(tmp_path, "api"), _repo(tmp_path, "web")
    shell = FakeShell({
        "api": _clean(status=" M a.py\n"),
        "web": _clean(origin="beefbeefbeef", head="oldsha", contains=[]),
    })
    msg = blocked_message(inspect(FakeTask({"api": api, "web": web}), shell))
    assert "uncommitted changes in api" in msg
    assert "unpulled commits on origin in web" in msg
    assert "mship commit" in msg and "pull --ff-only" in msg


# --- what must be pushed ----------------------------------------------------

def test_a_clean_branch_missing_from_origin_is_pushed(tmp_path):
    """The case that fails outright today: nothing pushes before `mship finish`."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(origin=None)})
    pre = inspect(FakeTask({"api": api}), shell)
    assert pre.ok and [s.push_reason for s in pre.to_push] == ["not on origin"]

    pushed, err = push(pre, shell)
    assert err is None and pushed == ["api"]
    assert shell.pushes and "push -u origin feat/x" in shell.pushes[0][0]


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


# --- what must only warn ----------------------------------------------------

def test_untracked_files_warn_rather_than_block(tmp_path):
    """They cannot change what a push carries, so blocking over a stray scratch
    file would be obstruction — but they will not exist on the run host."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status="?? scratch.txt\n")})
    pre = inspect(FakeTask({"api": api}), shell)
    assert pre.ok
    assert [s.repo for s in pre.untracked] == ["api"]


def test_untracked_alongside_tracked_still_blocks(tmp_path):
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": _clean(status="?? new.py\n M old.py\n")})
    pre = inspect(FakeTask({"api": api}), shell)
    assert not pre.ok


# --- scoping to the repos actually dispatched -------------------------------

def test_repos_scoping_ignores_a_dirty_repo_the_run_never_touches(tmp_path):
    """`--repos api` dispatches only api; work in progress in web cannot make
    api's run execute stale code, so refusing over it is pure obstruction."""
    api, web = _repo(tmp_path, "api"), _repo(tmp_path, "web")
    shell = FakeShell({
        "api": _clean(),
        "web": _clean(status=" M b.ts\n"),
    })
    pre = inspect(FakeTask({"api": api, "web": web}), shell, repos=["api"])
    assert pre.ok
    assert [s.repo for s in pre.states] == ["api"]


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


def test_real_repo_that_is_not_a_git_worktree_blocks(tmp_path):
    """`git status` exits non-zero with empty stdout here — the shape that read
    as a clean tree."""
    plain = tmp_path / "plain"
    plain.mkdir()
    pre = inspect(FakeTask({"api": plain}), RealShell())
    assert [s.blocked_reason for s in pre.blocked] == [UNREADABLE]
