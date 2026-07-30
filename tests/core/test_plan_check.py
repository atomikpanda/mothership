from mship.core.plan_check import (
    AxisVerdict,
    Flag,
    PlanCheckResult,
    PlanCheckStore,
    flags_from_verdicts,
    plan_hash,
)


def test_plan_hash_stable_across_trailing_whitespace_only_edits():
    a = "line one   \nline two\n"
    b = "line one\nline two   \n"
    assert plan_hash(a) == plan_hash(b)


def test_plan_hash_changes_on_content_edit():
    a = "line one\nline two\n"
    b = "line one\nline THREE\n"
    assert plan_hash(a) != plan_hash(b)


def test_flags_from_verdicts_only_not_covered():
    verdicts = [
        AxisVerdict(axis="rollback", verdict="covered", reason="handled in step 3"),
        AxisVerdict(axis="security", verdict="not-covered", reason="no auth check"),
        AxisVerdict(axis="perf", verdict="n-a", reason="no hot path"),
    ]
    flags = flags_from_verdicts(verdicts)
    assert len(flags) == 1
    assert flags[0] == Flag(axis="security", source="checker", reason="no auth check")


def test_flags_from_verdicts_none_when_all_covered_or_na():
    verdicts = [
        AxisVerdict(axis="rollback", verdict="covered", reason="ok"),
        AxisVerdict(axis="perf", verdict="n-a", reason="n/a"),
    ]
    assert flags_from_verdicts(verdicts) == []


def test_plan_check_store_roundtrip(tmp_path):
    store = PlanCheckStore(tmp_path)
    result = PlanCheckResult(
        task_slug="task-1",
        plan_hash=plan_hash("some plan text\n"),
        verdicts=[AxisVerdict(axis="rollback", verdict="not-covered", reason="missing")],
        flags=[Flag(axis="rollback", source="checker", reason="missing")],
    )
    saved_path = store.save(result)
    assert saved_path == store.path("task-1")
    assert saved_path.is_file()
    assert store.get("task-1") == result


def test_plan_check_store_get_absent_returns_none(tmp_path):
    store = PlanCheckStore(tmp_path)
    assert store.get("no-such-task") is None
