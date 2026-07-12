"""MOS-240: read-shim back-compat for the collapsed Spec status vocabulary.

Old persisted `specs/*.md` files carry statuses that no longer exist as first-class
values (`captured`, `drafting`, `needs_clarification`). `parse_spec` must map them
forward on read WITHOUT rewriting the file, so existing specs load cleanly and
round-trip without data loss.
"""
from __future__ import annotations

import pytest

from mship.core.spec_store import parse_spec, serialize_spec


def _spec_file(status: str, *, clarification_reason: str | None = None, extra: str = "") -> str:
    reason_line = f"clarification_reason: {clarification_reason}\n" if clarification_reason is not None else ""
    return (
        "---\n"
        "id: demo\n"
        "title: Demo\n"
        f"status: {status}\n"
        "created_at: 2026-06-13T10:00:00+00:00\n"
        "updated_at: 2026-06-13T10:00:00+00:00\n"
        f"{reason_line}"
        f"{extra}"
        "---\n"
        "## Problem\n\nbody text\n"
    )


@pytest.mark.parametrize("legacy", ["captured", "drafting"])
def test_legacy_captured_and_drafting_map_to_draft(legacy):
    spec = parse_spec(_spec_file(legacy))
    assert spec.status == "draft"


def test_legacy_needs_clarification_maps_to_needs_review_preserving_reason():
    spec = parse_spec(_spec_file("needs_clarification", clarification_reason="tighten scope"))
    assert spec.status == "needs_review"
    assert spec.clarification_reason == "tighten scope"


def test_legacy_needs_clarification_without_reason_gets_a_reason():
    # The new model expresses "needs clarification" via a non-null clarification_reason,
    # so a migrated needs_clarification spec that lacked one must gain one.
    spec = parse_spec(_spec_file("needs_clarification"))
    assert spec.status == "needs_review"
    assert spec.clarification_reason is not None


@pytest.mark.parametrize("status", ["draft", "needs_review", "approved", "dispatched", "implemented", "archived"])
def test_new_statuses_pass_through_unchanged(status):
    spec = parse_spec(_spec_file(status))
    assert spec.status == status


def test_read_shim_does_not_lose_other_fields():
    text = _spec_file("drafting", extra="task_slug: t1\nwork_item_id: wi-9\n")
    spec = parse_spec(text)
    assert spec.status == "draft"
    assert spec.task_slug == "t1"
    assert spec.work_item_id == "wi-9"
    assert spec.body == "## Problem\n\nbody text\n"


def test_migrated_spec_reserializes_with_new_status():
    # Loading old + saving should persist the forward-mapped status (no data loss,
    # forward migration on next write).
    spec = parse_spec(_spec_file("captured"))
    reparsed = parse_spec(serialize_spec(spec))
    assert reparsed.status == "draft"
