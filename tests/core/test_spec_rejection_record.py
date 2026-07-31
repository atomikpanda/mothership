"""record_rejection: a durable, append-only `rejected` journal event written
at request-changes time (CLI + serve), so a rejection survives a later
approve_spec() nulling clarification_reason on the spec itself."""
import json
from datetime import datetime, timezone

import pytest

from mship.core.log import LogManager
from mship.core.spec_transition import record_rejection


def test_record_rejection_writes_durable_rejected_entry(tmp_path):
    lm = LogManager(tmp_path / ".mothership" / "logs")
    record_rejection(
        lm, "spec-1", actor="alice", reason="metarepo not considered",
        now=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    entries = [e for e in lm.read("spec-1") if e.action == "rejected"]
    assert len(entries) == 1
    payload = json.loads(entries[0].message)
    assert payload == {"actor": "alice", "reason": "metarepo not considered"}


def test_rejection_entry_survives_later_appends(tmp_path):
    """Append-only: a later entry (e.g. a re-approval note) does not erase it."""
    lm = LogManager(tmp_path / ".mothership" / "logs")
    record_rejection(
        lm, "spec-1", actor="alice", reason="r1",
        now=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    lm.append("spec-1", "spec approved", action="approved")
    assert [e for e in lm.read("spec-1") if e.action == "rejected"]


@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_record_rejection_rejects_empty_reason(tmp_path, reason):
    """#447 review: every durable record must carry real reason text."""
    lm = LogManager(tmp_path / ".mothership" / "logs")
    with pytest.raises(ValueError):
        record_rejection(
            lm, "spec-1", actor="alice", reason=reason,
            now=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
    assert lm.read("spec-1") == []
