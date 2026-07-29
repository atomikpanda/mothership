"""Tests for mship.core.plan — shared plan resolution + validity (MOS-235)."""
from __future__ import annotations

from pathlib import Path

from mship.core.plan import (
    SEED_AXES,
    discover_plan_path,
    dispositioned_axes,
    missing_assumption_axes,
    plan_has_tasks,
    resolve_plan_path,
)

_PLAN = "# Plan\n\n<!-- mship:task id=1 -->\n### Task 1\n<!-- /mship:task -->\n"


def test_plan_has_tasks_true_when_anchor_present():
    assert plan_has_tasks(_PLAN) is True


def test_plan_has_tasks_false_when_no_anchor():
    assert plan_has_tasks("# Just prose, no tasks") is False


def test_discover_plan_path_matches_dated_slug(tmp_path):
    d = tmp_path / "docs" / "plans"
    d.mkdir(parents=True)
    p = d / "2026-07-12-add-labels.md"
    p.write_text(_PLAN)
    assert discover_plan_path(tmp_path, "add-labels", docs_dir="docs") == p


def test_resolve_plan_path_prefers_explicit(tmp_path):
    explicit = tmp_path / "custom" / "myplan.md"
    explicit.parent.mkdir(parents=True)
    explicit.write_text(_PLAN)
    got = resolve_plan_path("add-labels", str(explicit.relative_to(tmp_path)), tmp_path, "docs")
    assert got == explicit


def test_resolve_plan_path_falls_back_to_convention(tmp_path):
    d = tmp_path / "docs" / "plans"
    d.mkdir(parents=True)
    p = d / "add-labels.md"
    p.write_text(_PLAN)
    assert resolve_plan_path("add-labels", None, tmp_path, "docs") == p


def test_resolve_plan_path_none_when_missing(tmp_path):
    assert resolve_plan_path("add-labels", None, tmp_path, "docs") is None


def test_resolve_plan_path_rejects_absolute_escape(tmp_path):
    """An explicit plan_path outside the workspace is rejected — the gate/dispatch
    read this path, so never read files outside the workspace (Greptile security)."""
    outside = tmp_path.parent / "outside-plan.md"
    outside.write_text(_PLAN)
    assert resolve_plan_path("add-labels", str(outside), tmp_path, "docs") is None


def test_resolve_plan_path_rejects_dotdot_traversal(tmp_path):
    (tmp_path.parent / "esc-plan.md").write_text(_PLAN)
    assert resolve_plan_path("add-labels", "../esc-plan.md", tmp_path, "docs") is None


def test_plan_has_tasks_with_attributes():
    assert plan_has_tasks("<!-- mship:" "task id=1 acs=ac1 -->\nx\n<!-- /mship:" "task -->")


def test_dispositioned_axes_parses_block_with_em_dash_and_hyphen():
    plan = (
        "## Assumptions checked\n"
        "- repo topology — metarepo; covers clone across N repos\n"
        "- credential locus - N/A, no credential handling\n"
        "- execution locus -- cloud worker\n\n"
        "## Approach\nsomething\n"
    )
    assert dispositioned_axes(plan) == {"repo topology", "credential locus", "execution locus"}


def test_dispositioned_axes_no_block_returns_empty():
    assert dispositioned_axes("## Approach\nno assumptions block here\n") == set()


def test_dispositioned_axes_normalizes_case_and_whitespace():
    plan = "## Assumptions Checked\n- Repo   Topology — meta\n"
    assert dispositioned_axes(plan) == {"repo topology"}


def test_seed_axes_has_seven_including_repo_topology():
    assert len(SEED_AXES) == 7
    assert "repo topology" in SEED_AXES


_FULL_BLOCK = "## Assumptions checked\n" + "".join(
    f"- {a} — disposition line\n" for a in SEED_AXES
)


def test_missing_none_when_all_dispositioned():
    assert missing_assumption_axes(_FULL_BLOCK, SEED_AXES) == []


def test_missing_lists_omitted_axis_in_expected_order():
    partial = "## Assumptions checked\n- repo topology — meta\n- execution locus — cloud\n"
    missing = missing_assumption_axes(partial, SEED_AXES)
    assert "credential locus" in missing
    assert missing == [a for a in SEED_AXES if a not in {"repo topology", "execution locus"}]


def test_na_disposition_counts_as_covered():
    block = "## Assumptions checked\n" + "".join(f"- {a} — N/A\n" for a in SEED_AXES)
    assert missing_assumption_axes(block, SEED_AXES) == []


def test_no_block_reports_all_expected_missing():
    assert missing_assumption_axes("## Approach\nx\n", SEED_AXES) == list(SEED_AXES)


def test_unfilled_bracket_placeholder_does_not_count():
    """A copied-but-unfilled template placeholder ([...]/<...>) must NOT parse as
    a real disposition (Greptile #448) — else an untouched template greenlights."""
    plan = (
        "## Assumptions checked\n"
        "- repo topology — [covered/N/A: one line]\n"
        "- credential locus — <covered/N/A: one line>\n"
    )
    assert dispositioned_axes(plan) == set()


def test_unfilled_template_reports_all_axes_missing():
    """The writing-plans template, copied verbatim with every row left as a
    placeholder, must report ALL seed axes missing (ok would be false)."""
    block = "## Assumptions checked\n" + "".join(
        f"- {a} — [covered/N/A: one line]\n" for a in SEED_AXES
    )
    assert missing_assumption_axes(block, SEED_AXES) == list(SEED_AXES)


def test_real_disposition_containing_brackets_still_counts():
    """A real disposition that merely contains brackets still counts."""
    plan = "## Assumptions checked\n- repo topology — covered [see #123], metarepo\n"
    assert dispositioned_axes(plan) == {"repo topology"}


def test_filled_but_bracketed_disposition_counts():
    """A filled disposition that keeps the template's brackets but replaced the
    `covered/N/A` choice with real content counts — only the unfilled slash-form
    marker is rejected, not brackets per se (Greptile #448 follow-up)."""
    plan = "## Assumptions checked\n- repo topology — [covered: metarepo handles clones across repos]\n"
    assert dispositioned_axes(plan) == {"repo topology"}
