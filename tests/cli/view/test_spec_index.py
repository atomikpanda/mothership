from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.cli.view._spec_index import SpecIndexApp
from mship.core.state import Task, WorkspaceState


def _write_spec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _task(slug: str, **over) -> Task:
    base = dict(
        slug=slug, description=slug, phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["mothership"], worktrees={}, branch=f"feat/{slug}",
    )
    base.update(over)
    return Task(**base)


@pytest.mark.asyncio
async def test_spec_index_preserves_scroll_on_refresh(tmp_path: Path):
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    # Seed many specs so the table is scrollable.
    for i in range(30):
        (specs / f"s{i:02d}.md").write_text(f"# Spec {i}\n")

    state = WorkspaceState()
    app = SpecIndexApp(workspace_root=tmp_path, state=state, watch=False, interval=1.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Scroll the table down.
        app._table.scroll_to(x=0, y=10, animate=False)
        await pilot.pause()
        y_before = app._table.scroll_y
        assert y_before > 0

        # Trigger a refresh — same entries, scroll should be preserved.
        app._refresh_index()
        await pilot.pause()
        assert app._table.scroll_y == y_before, (
            f"scroll yanked: before={y_before}, after={app._table.scroll_y}"
        )


@pytest.mark.asyncio
async def test_spec_index_uses_live_state_on_compose(tmp_path: Path):
    """compose() must call state_loader so it reflects current state, not a stale snapshot."""
    wt = tmp_path / "wt-a"
    _write_spec(wt / "docs" / "superpowers" / "specs" / "live.md", "# Live\n")

    initial_state = WorkspaceState()  # empty — no tasks
    live_state = WorkspaceState(tasks={"a": _task("a", worktrees={"mothership": wt})})

    app = SpecIndexApp(
        workspace_root=tmp_path, state=initial_state,
        state_loader=lambda: live_state, watch=False, interval=1.0,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "live.md" in app.row_filenames()
