import pytest

from mship.core.view.queue import QueueItem
from mship.cli.view.queue import QueueView


def _items():
    return [
        QueueItem(kind="spec-needs-review", key="spec:wi-1", workspace="ws",
                  work_item_id="wi-1", work_item_title="Overhaul",
                  phase="shaping", spec_id="spec-1"),
        QueueItem(kind="blocked-task", key="block:a", workspace="ws",
                  work_item_id="wi-1", work_item_title="Overhaul",
                  phase="in_flight", task_slug="a",
                  blocked_reason="waiting on API key"),
        QueueItem(kind="pr-awaiting", key="pr:b:r", workspace="ws",
                  work_item_id="wi-1", work_item_title="Overhaul",
                  phase="review", task_slug="b", repo="r",
                  pr_url="https://gh/pr/9"),
    ]


@pytest.mark.asyncio
async def test_queue_view_lists_every_attention_item_with_header():
    view = QueueView(_items())
    async with view.run_test() as pilot:
        await pilot.pause()
        labels = view.list_labels()
        assert any("needs-review" in l for l in labels)
        assert any("blocked" in l for l in labels)
        assert any(l.startswith("[PR]") for l in labels)
        assert "queue" in view.header_text().lower()
        assert "3" in view.header_text()
        # First row (spec) detail shows the deferred-action note + spec id.
        assert "spec-1" in view.detail_text()


@pytest.mark.asyncio
async def test_queue_view_detail_follows_highlight():
    view = QueueView(_items())
    async with view.run_test() as pilot:
        await pilot.pause()
        view._master.focus()
        await pilot.pause()
        await pilot.press("j")  # -> blocked task
        await pilot.pause()
        assert "waiting on API key" in view.detail_text()
        await pilot.press("j")  # -> PR
        await pilot.pause()
        assert "https://gh/pr/9" in view.detail_text()


@pytest.mark.asyncio
async def test_queue_view_empty_is_safe():
    view = QueueView([])
    async with view.run_test() as pilot:
        await pilot.pause()
        assert view.list_labels() == []
        assert view.detail_text() == ""
        assert "0 needing attention" in view.header_text()
