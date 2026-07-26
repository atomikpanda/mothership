"""`unevidenced_warning`: the phase-transition hint for acceptance criteria
carrying no evidence. Extends the existing `mship spec evidence` remedy with
`mship capture --evidence` — but only for repos that actually define a capture
target, since suggesting a command that cannot run is worse than saying
nothing.
"""
from datetime import datetime, timezone

from mship.core.phase import unevidenced_warning
from mship.core.spec import AcceptanceCriterion, AcceptanceEvidence, Spec


def _spec_with(*criteria: AcceptanceCriterion) -> Spec:
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    return Spec(
        id="my-spec", title="My spec", status="approved",
        created_at=now, updated_at=now,
        acceptance_criteria=list(criteria),
    )


def test_hint_names_capture_when_a_repo_has_a_capture_target():
    spec = _spec_with(AcceptanceCriterion(id="ac1", text="renders"))
    msg = unevidenced_warning(spec, capture_repos=["ground-control"])
    assert "mship capture --evidence" in msg
    assert "ground-control" in msg


def test_hint_omits_capture_when_no_repo_defines_one():
    spec = _spec_with(AcceptanceCriterion(id="ac1", text="renders"))
    msg = unevidenced_warning(spec, capture_repos=[])
    assert "mship capture" not in msg
    # The original remedy still fires.
    assert "mship spec evidence" in msg


def test_no_warning_when_every_criterion_has_evidence():
    spec = _spec_with(
        AcceptanceCriterion(
            id="ac1", text="renders",
            evidence=[AcceptanceEvidence(kind="artifact", ref="abc123.png")],
        )
    )
    assert unevidenced_warning(spec, capture_repos=["ground-control"]) == ""
