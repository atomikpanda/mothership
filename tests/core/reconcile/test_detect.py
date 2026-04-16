from datetime import datetime, timezone
from mship.core.reconcile.detect import (
    UpstreamState, PRSnapshot, GitSnapshot, detect_one, detect_many,
)


def _task_snap(head="feat/foo", state="OPEN", base="main", merge_sha=None, url="https://x/pr/1", updated_at="2026-04-16T00:00:00Z"):
    return PRSnapshot(
        head_ref=head, state=state, base_ref=base,
        merge_commit=merge_sha, url=url, updated_at=updated_at,
    )


def test_detect_in_sync():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(),
        git=GitSnapshot(has_upstream=True, behind=0, ahead=3),
    )
    assert r.state == UpstreamState.in_sync


def test_detect_merged():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(state="MERGED", merge_sha="abc123"),
        git=GitSnapshot(has_upstream=True, behind=0, ahead=0),
    )
    assert r.state == UpstreamState.merged
    assert r.pr_url == "https://x/pr/1"


def test_detect_closed():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(state="CLOSED"),
        git=GitSnapshot(has_upstream=True, behind=0, ahead=0),
    )
    assert r.state == UpstreamState.closed


def test_detect_diverged_when_remote_ahead():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(),
        git=GitSnapshot(has_upstream=True, behind=4, ahead=1),
    )
    assert r.state == UpstreamState.diverged


def test_detect_base_changed():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(base="develop"),
        git=GitSnapshot(has_upstream=True, behind=0, ahead=1),
    )
    assert r.state == UpstreamState.base_changed


def test_detect_missing_when_no_pr():
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=None,
        git=GitSnapshot(has_upstream=False, behind=0, ahead=0),
    )
    assert r.state == UpstreamState.missing


def test_detect_many_maps_per_slug():
    result = detect_many(
        tasks=[
            ("a", "feat/a", "main"),
            ("b", "feat/b", "main"),
        ],
        pr_by_head={
            "feat/a": _task_snap(head="feat/a", state="MERGED", merge_sha="x"),
            "feat/b": _task_snap(head="feat/b"),
        },
        git_by_branch={
            "feat/a": GitSnapshot(has_upstream=True, behind=0, ahead=0),
            "feat/b": GitSnapshot(has_upstream=True, behind=0, ahead=2),
        },
    )
    assert result["a"].state == UpstreamState.merged
    assert result["b"].state == UpstreamState.in_sync


def test_detect_precedence_merged_beats_diverged():
    """When PR is merged, we don't care about local divergence."""
    r = detect_one(
        task_branch="feat/foo", task_base="main",
        pr=_task_snap(state="MERGED", merge_sha="x"),
        git=GitSnapshot(has_upstream=True, behind=99, ahead=99),
    )
    assert r.state == UpstreamState.merged
