"""Tests for mship.core.plan — shared plan resolution + validity (MOS-235)."""
from __future__ import annotations

from pathlib import Path

from mship.core.plan import discover_plan_path, plan_has_tasks, resolve_plan_path

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
