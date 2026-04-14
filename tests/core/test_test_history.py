import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.test_history import (
    write_run, read_run, latest_iteration,
    compute_diff, prune,
)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / ".mothership"
    d.mkdir()
    return d


def _results(**kw):
    """Helper: build the per-repo results dict."""
    return {
        name: {"status": status, "duration_ms": dur, "exit_code": ec, "stderr_tail": tail}
        for name, (status, dur, ec, tail) in kw.items()
    }


def test_write_run_creates_iteration_file_and_latest_pointer(state_dir):
    results = _results(shared=("pass", 1200, 0, None))
    path = write_run(
        state_dir, "t", iteration=1,
        started_at=datetime(2026, 4, 14, 12, tzinfo=timezone.utc),
        duration_ms=1234, results=results,
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["iteration"] == 1
    assert data["duration_ms"] == 1234
    assert data["repos"]["shared"]["status"] == "pass"
    # latest pointer
    latest = state_dir / "test-runs" / "t" / "latest.json"
    assert latest.exists()
    assert json.loads(latest.read_text())["iteration"] == 1


def test_read_run_round_trip(state_dir):
    write_run(
        state_dir, "t", iteration=2,
        started_at=datetime(2026, 4, 14, 12, tzinfo=timezone.utc),
        duration_ms=100,
        results=_results(api=("fail", 99, 1, "boom")),
    )
    run = read_run(state_dir, "t", 2)
    assert run is not None
    assert run["repos"]["api"]["stderr_tail"] == "boom"


def test_read_run_missing_returns_none(state_dir):
    assert read_run(state_dir, "t", 42) is None


def test_latest_iteration_returns_highest(state_dir):
    for i in (1, 2, 3):
        write_run(state_dir, "t", iteration=i,
                   started_at=datetime.now(timezone.utc),
                   duration_ms=0, results=_results())
    assert latest_iteration(state_dir, "t") == 3


def test_latest_iteration_none_for_missing_task(state_dir):
    assert latest_iteration(state_dir, "t") is None


def test_compute_diff_first_run_tags_all_as_first_run():
    current = {"iteration": 1, "repos": {"a": {"status": "pass"}}}
    result = compute_diff(current, previous=None, pre_previous=None)
    assert result["previous_iteration"] is None
    assert result["tags"] == {"a": "first run"}
    assert result["summary"]["new_failures"] == []


def test_compute_diff_pass_to_pass_is_still_passing():
    prev = {"iteration": 1, "repos": {"a": {"status": "pass"}}}
    curr = {"iteration": 2, "repos": {"a": {"status": "pass"}}}
    d = compute_diff(curr, prev, None)
    assert d["tags"]["a"] == "still passing"


def test_compute_diff_pass_to_fail_is_new_failure_without_pre_previous():
    prev = {"iteration": 1, "repos": {"a": {"status": "pass"}}}
    curr = {"iteration": 2, "repos": {"a": {"status": "fail"}}}
    d = compute_diff(curr, prev, None)
    assert d["tags"]["a"] == "new failure"
    assert d["summary"]["new_failures"] == ["a"]


def test_compute_diff_pass_to_fail_is_regression_when_pre_previous_also_passed():
    pre = {"iteration": 1, "repos": {"a": {"status": "pass"}}}
    prev = {"iteration": 2, "repos": {"a": {"status": "pass"}}}
    curr = {"iteration": 3, "repos": {"a": {"status": "fail"}}}
    d = compute_diff(curr, prev, pre)
    assert d["tags"]["a"] == "regression"
    assert d["summary"]["regressions"] == ["a"]


def test_compute_diff_fail_to_pass_is_fix():
    prev = {"iteration": 1, "repos": {"a": {"status": "fail"}}}
    curr = {"iteration": 2, "repos": {"a": {"status": "pass"}}}
    d = compute_diff(curr, prev, None)
    assert d["tags"]["a"] == "fix"
    assert d["summary"]["fixes"] == ["a"]


def test_compute_diff_fail_to_fail_is_still_failing():
    prev = {"iteration": 1, "repos": {"a": {"status": "fail"}}}
    curr = {"iteration": 2, "repos": {"a": {"status": "fail"}}}
    d = compute_diff(curr, prev, None)
    assert d["tags"]["a"] == "still failing"


def test_prune_keeps_newest_n(state_dir):
    for i in range(1, 26):
        write_run(state_dir, "t", iteration=i,
                   started_at=datetime.now(timezone.utc),
                   duration_ms=0, results=_results())
    prune(state_dir, "t", keep=20)
    remaining = sorted(
        int(p.stem) for p in (state_dir / "test-runs" / "t").iterdir()
        if p.stem.isdigit()
    )
    assert remaining == list(range(6, 26))
    # latest.json preserved
    assert (state_dir / "test-runs" / "t" / "latest.json").exists()


def test_write_run_writes_stdout_stderr_artifacts(state_dir):
    results = _results(api=("fail", 500, 1, "boom"))
    streams = {"api": ("hello stdout", "hello stderr")}
    path = write_run(
        state_dir, "t", iteration=3,
        started_at=datetime(2026, 4, 14, 12, tzinfo=timezone.utc),
        duration_ms=500, results=results, streams=streams,
    )
    run_dir = state_dir / "test-runs" / "t"

    stdout_file = run_dir / "3.api.stdout"
    stderr_file = run_dir / "3.api.stderr"
    assert stdout_file.exists(), "stdout artifact not written"
    assert stderr_file.exists(), "stderr artifact not written"
    assert stdout_file.read_text() == "hello stdout"
    assert stderr_file.read_text() == "hello stderr"

    data = json.loads(path.read_text())
    assert data["repos"]["api"]["stdout_path"] == str(stdout_file.resolve())
    assert data["repos"]["api"]["stderr_path"] == str(stderr_file.resolve())


def test_write_run_no_streams_backward_compatible(state_dir):
    """write_run without streams must not add stdout_path/stderr_path."""
    results = _results(api=("pass", 100, 0, None))
    path = write_run(
        state_dir, "t", iteration=1,
        started_at=datetime(2026, 4, 14, 12, tzinfo=timezone.utc),
        duration_ms=100, results=results,
    )
    data = json.loads(path.read_text())
    assert "stdout_path" not in data["repos"]["api"]
    assert "stderr_path" not in data["repos"]["api"]


def test_prune_deletes_artifacts_alongside_iteration_files(state_dir):
    """Seed 25 iterations with artifacts; after prune(keep=20), first 5 gone."""
    for i in range(1, 26):
        results = _results(svc=("pass", 100, 0, None))
        write_run(
            state_dir, "t", iteration=i,
            started_at=datetime.now(timezone.utc),
            duration_ms=0, results=results,
            streams={"svc": (f"stdout-{i}", f"stderr-{i}")},
        )

    prune(state_dir, "t", keep=20)

    run_dir = state_dir / "test-runs" / "t"
    # Iterations 1-5 and their artifacts must be gone
    for i in range(1, 6):
        assert not (run_dir / f"{i}.json").exists(), f"{i}.json should be pruned"
        assert not (run_dir / f"{i}.svc.stdout").exists(), f"{i}.svc.stdout should be pruned"
        assert not (run_dir / f"{i}.svc.stderr").exists(), f"{i}.svc.stderr should be pruned"

    # Iterations 6-25 must remain
    for i in range(6, 26):
        assert (run_dir / f"{i}.json").exists(), f"{i}.json should remain"
        assert (run_dir / f"{i}.svc.stdout").exists(), f"{i}.svc.stdout should remain"
        assert (run_dir / f"{i}.svc.stderr").exists(), f"{i}.svc.stderr should remain"
