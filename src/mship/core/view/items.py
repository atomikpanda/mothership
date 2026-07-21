"""Pure row formatters for `mship view items` — the WorkItems picker. Mirrors
core/view/queue.py: label/detail/render_text over the shared WorkItemSummary
index (id/title/derived-phase/attention), no store wiring here."""
from __future__ import annotations

from mship.core.view.workitem_index import WorkItemSummary


def _attention_marker(s: WorkItemSummary) -> str:
    a = s.attention
    if a.needs_approval or a.needs_decision or a.blocked or a.needs_review:
        return "!"
    return " "


def items_label(s: WorkItemSummary) -> str:
    return f"{_attention_marker(s)} {s.id}  {s.title or '(untitled)'}  [{s.phase}]"


def items_detail(s: WorkItemSummary) -> str:
    a = s.attention
    lines = [
        f"{s.id}  {s.title}",
        f"phase: {s.phase}",
        f"spec: {s.spec_id or '(none)'}",
        f"tasks: {', '.join(s.task_slugs) or '(none)'}",
        f"attention: approval={a.needs_approval} decision={a.needs_decision} "
        f"blocked={a.blocked} review={a.needs_review}",
    ]
    return "\n".join(lines)


def items_render_text(summaries: list[WorkItemSummary]) -> str:
    if not summaries:
        return "No work items."
    return "\n".join(items_label(s) for s in summaries)
