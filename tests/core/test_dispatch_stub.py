from mship.core.dispatch_stub import STUB_FIELDS, build_stub
from tests.core.test_sdd_store import _record  # reuse the fixture factory


def test_stub_contains_exactly_the_closed_fields():
    rec = _record()
    stub = build_stub(rec, record_path="/ws/.mothership/sdd/wi-1/my-task/record.json")
    labels = tuple(line.split(":", 1)[0] for line in stub.splitlines())
    # Every field present, in order, and NOTHING else — a new field must be
    # added to STUB_FIELDS and consciously accepted here (that's the point).
    assert labels == STUB_FIELDS


def test_stub_carries_no_prompt_content():
    """Spec ac3: controller stdout is a closed set — no task body, no template
    boilerplate, no acceptance text, no subagent-only prompt content."""
    rec = _record()
    stub = build_stub(rec, record_path="/p/record.json")
    assert len(stub.splitlines()) <= 8
    for leaked in (
        "Work from (mandatory)",     # template section headings
        "Conventions (recap)",
        "Report back",
        "Your instruction",
    ):
        assert leaked not in stub


def test_stub_emit_line_is_runnable_from_worktree():
    rec = _record()
    stub = build_stub(rec, record_path="/p/record.json")
    assert "mship dispatch --emit" in stub
