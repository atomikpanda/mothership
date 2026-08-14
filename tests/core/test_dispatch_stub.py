import pytest

from mship.core.dispatch_stub import STUB_FIELDS, build_stub
from tests.core.test_sdd_store import _record  # reuse the fixture factory


@pytest.mark.parametrize("mode", ["implementer", "standalone", "reviewer"])
def test_stub_contains_exactly_the_closed_fields_for_portable_defaults(mode):
    rec = _record(mode=mode, model="inherit")
    stub = build_stub(rec, record_path="/ws/.mothership/sdd/wi-1/my-task/record.json")
    labels = tuple(line.split(":", 1)[0] for line in stub.splitlines())
    # Every field present, in order, and NOTHING else — a new field must be
    # added to STUB_FIELDS and consciously accepted here (that's the point).
    assert labels == STUB_FIELDS == ("record", "model", "mode", "worktree", "emit")
    for provider_tier in ("sonnet", "haiku", "opus"):
        assert provider_tier not in stub


def test_stub_emits_explicit_model_verbatim_in_both_model_bearing_lines():
    value = "vendor/custom-tier:2026-08"
    stub = build_stub(_record(model=value), record_path="/p/record.json")
    lines = stub.splitlines()

    assert lines[1] == f"model: {value}"
    assert f"model={value}" in lines[4]
    assert stub.count(value) == 2


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
