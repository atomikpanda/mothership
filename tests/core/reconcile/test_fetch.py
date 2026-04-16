from pathlib import Path

from mship.core.reconcile.fetch import (
    FetchError,
    parse_gh_pr_list,
    gh_search_query,
    collect_git_snapshots,
)
from mship.core.reconcile.detect import GitSnapshot


def test_gh_search_query_ors_heads():
    q = gh_search_query(["feat/a", "feat/b"])
    assert "head:feat/a" in q
    assert "head:feat/b" in q


def test_parse_gh_pr_list_maps_fields():
    raw = [
        {
            "headRefName": "feat/a",
            "state": "MERGED",
            "baseRefName": "main",
            "mergeCommit": {"oid": "abc123"},
            "url": "https://github.com/o/r/pull/42",
            "updatedAt": "2026-04-16T00:00:00Z",
        },
        {
            "headRefName": "feat/b",
            "state": "OPEN",
            "baseRefName": "main",
            "mergeCommit": None,
            "url": "https://github.com/o/r/pull/43",
            "updatedAt": "2026-04-16T01:00:00Z",
        },
    ]
    parsed = parse_gh_pr_list(raw)
    assert parsed["feat/a"].state == "MERGED"
    assert parsed["feat/a"].merge_commit == "abc123"
    assert parsed["feat/b"].state == "OPEN"
    assert parsed["feat/b"].merge_commit is None


def test_parse_gh_pr_list_picks_most_recent_on_dup_head():
    raw = [
        {"headRefName": "feat/a", "state": "CLOSED", "baseRefName": "main",
         "mergeCommit": None, "url": "url-old", "updatedAt": "2026-01-01T00:00:00Z"},
        {"headRefName": "feat/a", "state": "OPEN", "baseRefName": "main",
         "mergeCommit": None, "url": "url-new", "updatedAt": "2026-04-16T00:00:00Z"},
    ]
    parsed = parse_gh_pr_list(raw)
    assert parsed["feat/a"].url == "url-new"


def test_parse_gh_pr_list_handles_missing_fields_gracefully():
    raw = [
        {"headRefName": "feat/a"},  # missing state etc.
        {"headRefName": "feat/b", "state": "OPEN", "baseRefName": "main",
         "mergeCommit": None, "url": "u", "updatedAt": "2026-04-16T00:00:00Z"},
    ]
    parsed = parse_gh_pr_list(raw)
    assert "feat/a" not in parsed
    assert "feat/b" in parsed


class _FakeGit:
    def __init__(self, per_branch):
        # per_branch: {branch: (behind, ahead)}
        self._per_branch = per_branch

    def run(self, args, cwd=None):
        if args[:2] == ["rev-parse", "--abbrev-ref"] and args[2].endswith("@{u}"):
            branch = args[2].removesuffix("@{u}")
            if branch in self._per_branch:
                return (0, f"origin/{branch}\n")
            return (1, "")
        if args[:3] == ["rev-list", "--left-right", "--count"]:
            branch = next(iter(self._per_branch))
            behind, ahead = self._per_branch[branch]
            return (0, f"{behind}\t{ahead}\n")
        return (0, "")


def test_collect_git_snapshots_uses_rev_list(tmp_path):
    worktrees_by_branch = {"feat/a": tmp_path}
    fake = _FakeGit({"feat/a": (2, 5)})
    snaps = collect_git_snapshots(worktrees_by_branch, runner=fake)
    assert snaps["feat/a"] == GitSnapshot(has_upstream=True, behind=2, ahead=5)


def test_collect_git_snapshots_no_upstream(tmp_path):
    worktrees_by_branch = {"feat/a": tmp_path}
    fake = _FakeGit({})
    snaps = collect_git_snapshots(worktrees_by_branch, runner=fake)
    assert snaps["feat/a"].has_upstream is False
    assert snaps["feat/a"].behind == 0


def test_fetch_error_raised_on_gh_missing(monkeypatch):
    from mship.core.reconcile.fetch import fetch_pr_snapshots
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    try:
        fetch_pr_snapshots(branches=["feat/a"])
    except FetchError as e:
        assert "gh" in str(e).lower()
    else:
        raise AssertionError("expected FetchError")
