from datetime import datetime, timezone

from mship.core.view.items import items_detail, items_label, items_render_text
from mship.core.view.workitem_index import Attention, WorkItemSummary


def _summary(**over):
    base = dict(
        id="wi-1", title="Overhaul", kind="feature", workspace="t",
        phase="in_flight",
        attention=Attention(needs_approval=True, needs_decision=False, blocked=False,
                            needs_review=False, blocked_tasks=0, total_tasks=1),
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        spec_id="spec-1", task_slugs=["a"], thread_ids=[],
    )
    base.update(over)
    return WorkItemSummary(**base)


def test_items_label_has_id_title_phase_and_attention_marker():
    label = items_label(_summary())
    assert "wi-1" in label and "Overhaul" in label and "[in_flight]" in label
    assert "!" in label  # needs_approval surfaces an attention marker


def test_items_label_no_attention_has_no_marker():
    label = items_label(_summary(attention=Attention(
        needs_approval=False, needs_decision=False, blocked=False,
        needs_review=False, blocked_tasks=0, total_tasks=1)))
    assert "!" not in label


def test_items_detail_lists_links():
    detail = items_detail(_summary())
    assert "spec-1" in detail and "a" in detail


def test_items_render_text_lists_all():
    text = items_render_text([_summary(id="wi-1"), _summary(id="wi-2", title="Second")])
    assert "wi-1" in text and "wi-2" in text and "Second" in text


def test_done_item_label_marked_as_no_tab():
    # A done WorkItem has no cockpit tab; the row must say so, non-done rows must not.
    done = items_label(_summary(phase="done"))
    assert "done (no tab)" in done
    not_done = items_label(_summary(phase="in_flight"))
    assert "no tab" not in not_done


def test_done_item_render_text_marked_as_no_tab():
    text = items_render_text([_summary(id="wi-1", phase="done")])
    assert "done (no tab)" in text


def test_done_item_detail_explains_no_tab():
    detail = items_detail(_summary(phase="done"))
    assert "no cockpit tab" in detail
    assert "no cockpit tab" not in items_detail(_summary(phase="in_flight"))
