"""The remote-run preflight: never dispatch a run that would execute stale code."""
from pathlib import Path

from mship.core.remote_preflight import blocked_message, inspect, push


class FakeTask:
    def __init__(self, worktrees, branch="feat/x"):
        self.worktrees = worktrees
        self.branch = branch


class FakeShell:
    """Answers git queries from a per-repo script keyed by path name."""

    def __init__(self, answers, push_rc=0, push_err=""):
        self._answers = answers
        self._push_rc = push_rc
        self._push_err = push_err
        self.pushes: list[tuple[str, Path]] = []

    def run(self, cmd, cwd=None, **kw):
        key = Path(cwd).name
        spec = self._answers[key]
        if "status --porcelain" in cmd:
            out, rc = spec["status"], 0
        elif "symbolic-full-name" in cmd:
            out, rc = ("origin/feat/x", 0) if spec["upstream"] else ("", 128)
        elif "rev-list --count" in cmd:
            out, rc = str(spec["ahead"]), 0
        elif cmd.startswith("git push"):
            self.pushes.append((cmd, Path(cwd)))
            out, rc = "", self._push_rc
        else:
            out, rc = "", 0

        class R:
            stdout = out
            stderr = self._push_err if cmd.startswith("git push") else ""
            returncode = rc
        return R()


def _repo(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- what must be refused ---------------------------------------------------

def test_tracked_changes_block_the_run(tmp_path):
    """The dangerous case: the remote would run the last pushed revision."""
    api = _repo(tmp_path, "api")
    task = FakeTask({"api": api})
    shell = FakeShell({"api": {"status": " M src/app.py\n", "upstream": True, "ahead": 0}})

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
    shell = FakeShell({"api": {"status": "M  src/app.py\n", "upstream": True, "ahead": 0}})
    assert not inspect(FakeTask({"api": api}), shell).ok


def test_every_dirty_repo_is_named_not_just_the_first(tmp_path):
    """A multi-repo task must not report one repo and leave the others to
    surface one at a time."""
    api, web = _repo(tmp_path, "api"), _repo(tmp_path, "web")
    shell = FakeShell({
        "api": {"status": " M a.py\n", "upstream": True, "ahead": 0},
        "web": {"status": " M b.ts\n", "upstream": True, "ahead": 0},
    })
    pre = inspect(FakeTask({"api": api, "web": web}), shell)
    assert sorted(s.repo for s in pre.blocked) == ["api", "web"]
    assert "api, web" in blocked_message(pre)


# --- what must be pushed ----------------------------------------------------

def test_a_clean_branch_with_no_upstream_is_pushed(tmp_path):
    """The case that fails outright today: nothing pushes before `mship finish`."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": {"status": "", "upstream": False, "ahead": 0}})
    pre = inspect(FakeTask({"api": api}), shell)
    assert pre.ok and [s.push_reason for s in pre.to_push] == ["no upstream"]

    pushed, err = push(pre, shell)
    assert err is None and pushed == ["api"]
    assert shell.pushes and "push -u origin feat/x" in shell.pushes[0][0]


def test_a_clean_branch_ahead_of_origin_is_pushed(tmp_path):
    """The dangerous-silent case: origin has an older commit."""
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": {"status": "", "upstream": True, "ahead": 3}})
    pre = inspect(FakeTask({"api": api}), shell)
    assert [s.push_reason for s in pre.to_push] == ["ahead of origin"]
    assert push(pre, shell)[1] is None


def test_an_up_to_date_repo_costs_no_push(tmp_path):
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": {"status": "", "upstream": True, "ahead": 0}})
    pre = inspect(FakeTask({"api": api}), shell)
    assert pre.to_push == []
    assert push(pre, shell) == ([], None)
    assert shell.pushes == []


def test_every_affected_repo_is_pushed(tmp_path):
    """A task has a branch per repo and the run host materializes each
    separately, so pushing one leaves the others stale."""
    api, web = _repo(tmp_path, "api"), _repo(tmp_path, "web")
    shell = FakeShell({
        "api": {"status": "", "upstream": False, "ahead": 0},
        "web": {"status": "", "upstream": True, "ahead": 2},
    })
    pre = inspect(FakeTask({"api": api, "web": web}), shell)
    pushed, err = push(pre, shell)
    assert err is None and sorted(pushed) == ["api", "web"]
    assert len(shell.pushes) == 2


def test_a_failed_push_stops_the_run(tmp_path):
    """Proceeding after a failed push is the exact failure being prevented."""
    api = _repo(tmp_path, "api")
    shell = FakeShell(
        {"api": {"status": "", "upstream": False, "ahead": 0}},
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
    shell = FakeShell({"api": {"status": "?? scratch.txt\n", "upstream": True, "ahead": 0}})
    pre = inspect(FakeTask({"api": api}), shell)
    assert pre.ok
    assert [s.repo for s in pre.untracked] == ["api"]


def test_untracked_alongside_tracked_still_blocks(tmp_path):
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": {"status": "?? new.py\n M old.py\n", "upstream": True, "ahead": 0}})
    pre = inspect(FakeTask({"api": api}), shell)
    assert not pre.ok


# --- robustness -------------------------------------------------------------

def test_a_missing_worktree_is_skipped_not_crashed_on(tmp_path):
    """`audit`/`prune` own missing-worktree reporting; preflight must not die."""
    api = _repo(tmp_path, "api")
    task = FakeTask({"api": api, "gone": tmp_path / "not-there"})
    shell = FakeShell({"api": {"status": "", "upstream": True, "ahead": 0}})
    assert [s.repo for s in inspect(task, shell).states] == ["api"]


def test_an_unparseable_ahead_count_pushes_rather_than_assuming(tmp_path):
    api = _repo(tmp_path, "api")
    shell = FakeShell({"api": {"status": "", "upstream": True, "ahead": "garbage"}})
    pre = inspect(FakeTask({"api": api}), shell)
    assert [s.repo for s in pre.to_push] == ["api"]
