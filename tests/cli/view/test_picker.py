from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from mship.core.state import Task, WorkspaceState, TestResult
from mship.core.view.task_index import build_task_index
from mship.cli.view._picker import TaskPicker, picker_rows


def _t(slug: str, **over) -> Task:
    base = dict(
        slug=slug, description=slug, phase="dev",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        affected_repos=["mothership"], worktrees={}, branch=f"feat/{slug}",
    )
    base.update(over)
    return Task(**base)


def test_picker_rows_contain_slug_phase_flags(tmp_path: Path):
    now = datetime.now(timezone.utc)
    state = WorkspaceState(tasks={
        "a": _t("a"),
        "b": _t("b", finished_at=now),
        "c": _t("c", blocked_reason="waiting on review"),
        "d": _t("d", test_results={"mothership": TestResult(status="fail", at=now)}),
    })
    index = build_task_index(state, tmp_path)
    rows = picker_rows(index)
    slugs = {r.slug: r for r in rows}
    assert "⚠ close" in slugs["b"].flags
    assert "🚫 blocked" in slugs["c"].flags
    assert "🧪 fail" in slugs["d"].flags
    assert slugs["a"].flags == ""


@pytest.mark.asyncio
async def test_picker_renders_all_rows(tmp_path: Path):
    state = WorkspaceState(tasks={"a": _t("a"), "b": _t("b")})
    index = build_task_index(state, tmp_path)
    app = TaskPicker(rows=picker_rows(index), extra_columns=())
    async with app.run_test() as pilot:
        await pilot.pause()
        labels = app.row_slugs()
        assert labels == ["a", "b"] or labels == ["b", "a"]
