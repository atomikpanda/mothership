from mship.core.assumptions import AssumptionRow
from mship.core.plan_check import (
    AxisVerdict,
    Flag,
    PlanCheckResult,
    PlanCheckStore,
    cross_check,
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


def _row(axis: str, triggers: str = "") -> AssumptionRow:
    return AssumptionRow(axis=axis, options="a / b", position="**a**", triggers=triggers)


def test_flags_from_verdicts_only_not_covered():
    rows = [_row("rollback"), _row("security"), _row("perf")]
    verdicts = [
        AxisVerdict(axis="rollback", verdict="covered", reason="handled in step 3"),
        AxisVerdict(axis="security", verdict="not-covered", reason="no auth check"),
        AxisVerdict(axis="perf", verdict="n-a", reason="no hot path"),
    ]
    flags = flags_from_verdicts(verdicts, rows)
    assert len(flags) == 1
    assert flags[0] == Flag(axis="security", source="checker", reason="no auth check")


def test_flags_from_verdicts_none_when_all_covered_or_na():
    rows = [_row("rollback"), _row("perf")]
    verdicts = [
        AxisVerdict(axis="rollback", verdict="covered", reason="ok"),
        AxisVerdict(axis="perf", verdict="n-a", reason="n/a"),
    ]
    assert flags_from_verdicts(verdicts, rows) == []


def test_flags_from_verdicts_missing_row_is_flagged():
    """A canonical row the checker OMITS a verdict for must still flag — an
    un-dispositioned row can't silently pass the gate (Greptile #451)."""
    rows = [_row("rollback"), _row("security")]
    verdicts = [AxisVerdict(axis="rollback", verdict="covered", reason="ok")]
    flags = flags_from_verdicts(verdicts, rows)
    assert len(flags) == 1
    assert flags[0].axis == "security"
    assert flags[0].source == "checker"


def test_flags_from_verdicts_ignores_unknown_axis():
    """A verdict for an axis that is NOT a current row (invented/misspelled) must
    NOT create a phantom flag the operator has to approve (Greptile #451)."""
    rows = [_row("rollback")]
    verdicts = [
        AxisVerdict(axis="rollback", verdict="covered", reason="ok"),
        AxisVerdict(axis="totally-made-up", verdict="not-covered", reason="phantom"),
    ]
    assert flags_from_verdicts(verdicts, rows) == []


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


def _repo_topology_row() -> AssumptionRow:
    return AssumptionRow(
        axis="repo topology",
        options="single / mono / meta",
        position="**meta**",
        triggers="git/*, workspace/*, clone, branch, push",
    )


def test_cross_check_flags_triggered_axis_declared_na():
    rows = [_repo_topology_row()]
    verdicts = [AxisVerdict(axis="repo topology", verdict="n-a", reason="not relevant")]
    flags = cross_check(
        verdicts, rows, plan_text="we'll add a git/clone step", task_text="", affected_repos=[]
    )
    assert len(flags) == 1
    assert flags[0].axis == "repo topology"
    assert flags[0].source == "cross-check"


def test_cross_check_no_flag_when_triggers_dont_match():
    rows = [_repo_topology_row()]
    verdicts = [AxisVerdict(axis="repo topology", verdict="n-a", reason="not relevant")]
    flags = cross_check(
        verdicts,
        rows,
        plan_text="just some unrelated prose",
        task_text="nothing here either",
        affected_repos=[],
    )
    assert flags == []


def test_cross_check_no_flag_when_triggered_and_covered():
    rows = [_repo_topology_row()]
    verdicts = [AxisVerdict(axis="repo topology", verdict="covered", reason="handled")]
    flags = cross_check(
        verdicts, rows, plan_text="we'll add a git/clone step", task_text="", affected_repos=[]
    )
    assert flags == []


def test_cross_check_no_flag_when_triggered_and_not_covered():
    """not-covered is already surfaced by flags_from_verdicts; cross-check shouldn't
    duplicate it."""
    rows = [_repo_topology_row()]
    verdicts = [AxisVerdict(axis="repo topology", verdict="not-covered", reason="missing")]
    flags = cross_check(
        verdicts, rows, plan_text="we'll add a git/clone step", task_text="", affected_repos=[]
    )
    assert flags == []


def test_cross_check_no_longer_flags_missing_verdict():
    """A row with no verdict is a COMPLETENESS failure owned by
    flags_from_verdicts (which flags every un-dispositioned row, triggered or
    not). cross_check must NOT also flag it, or a triggered+missing row would be
    flagged twice and the operator would have to approve the same row twice
    (Greptile #451)."""
    rows = [_repo_topology_row()]
    flags = cross_check(
        [], rows, plan_text="we'll add a git/clone step", task_text="", affected_repos=[]
    )
    assert flags == []
    # The completeness net still catches it:
    assert len(flags_from_verdicts([], rows)) == 1


def test_cross_check_prefix_token_matches_by_prefix_not_substring():
    rows = [_repo_topology_row()]
    verdicts = [AxisVerdict(axis="repo topology", verdict="n-a", reason="not relevant")]
    # "workspace/*" should match "workspace/foo" but not a bare "workspace" mention.
    flags = cross_check(
        verdicts,
        rows,
        plan_text="touches workspace/foo module",
        task_text="",
        affected_repos=[],
    )
    assert len(flags) == 1

    flags_no_match = cross_check(
        verdicts,
        rows,
        plan_text="this workspace needs review",
        task_text="",
        affected_repos=[],
    )
    assert flags_no_match == []


def test_cross_check_case_insensitive_substring_match():
    rows = [_repo_topology_row()]
    verdicts = [AxisVerdict(axis="repo topology", verdict="n-a", reason="not relevant")]
    flags = cross_check(
        verdicts, rows, plan_text="we ran a BRANCH cleanup", task_text="", affected_repos=[]
    )
    assert len(flags) == 1


def test_cross_check_matches_affected_repos_entry():
    rows = [_repo_topology_row()]
    verdicts = [AxisVerdict(axis="repo topology", verdict="n-a", reason="not relevant")]
    flags = cross_check(
        verdicts,
        rows,
        plan_text="nothing relevant here",
        task_text="",
        affected_repos=["clone"],
    )
    assert len(flags) == 1


def test_cross_check_never_removes_checker_flags():
    """cross_check only returns cross-check flags; combining with flags_from_verdicts
    is additive at the caller."""
    rows = [_repo_topology_row()]
    verdicts = [AxisVerdict(axis="repo topology", verdict="not-covered", reason="missing")]
    checker_flags = flags_from_verdicts(verdicts, rows)
    cc_flags = cross_check(
        verdicts, rows, plan_text="we'll add a git/clone step", task_text="", affected_repos=[]
    )
    assert len(checker_flags) == 1
    assert cc_flags == []


def test_cross_check_prefix_matches_segment_not_midword():
    """`run/*` must match `run/foo` (real segment) but NOT `prerun/config`
    (mid-word) — a false flag spends operator attention (#444 backtest)."""
    from mship.core.plan_check import _triggers_match
    assert _triggers_match("run/*", "touches run/config today", "", []) is True
    assert _triggers_match("run/*", "touches prerun/config today", "", []) is False


def test_cross_check_plain_token_matches_on_word_boundary_only():
    """`run` matches the whole word `run` but NOT `brunch`; `UI` not in `build`."""
    from mship.core.plan_check import _triggers_match
    assert _triggers_match("run", "we run the worker", "", []) is True
    assert _triggers_match("run", "we had brunch after", "", []) is False
    assert _triggers_match("ui", "the review UI card", "", []) is True
    assert _triggers_match("ui", "we build the thing", "", []) is False
