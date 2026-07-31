"""L0 canary (ac9): 'a flag rate near zero is a defect'. A known-bad plan — the
canonical failure this whole system exists to catch: a git feature designed
considering only single-repo / monorepo, with metarepo (the product's core
divergence) ignored — MUST still be flagged. If the seed rows or the deterministic
cross-check ever regress so this passes silently, these tests fail loudly."""
from mship.core.assumptions import SEED_ROWS
from mship.core.plan_check import AxisVerdict, cross_check, flags_from_verdicts

# A plan whose text triggers the repo-topology axis (mentions git/clone) but whose
# disposition wrongly declares it n/a — the metarepo option was never considered.
_KNOWN_BAD_PLAN = (
    "# Plan\n\nAdd a git clone step and handle the single-repo and monorepo layouts.\n"
)


def test_canary_cross_check_flags_ignored_metarepo_divergence():
    rows = list(SEED_ROWS)
    verdicts = [AxisVerdict(axis="repo topology", verdict="n-a",
                            reason="only single/mono considered")]
    flags = cross_check(verdicts, rows, plan_text=_KNOWN_BAD_PLAN, task_text="", affected_repos=[])
    assert any(f.axis == "repo topology" for f in flags), (
        "CANARY TRIPPED: the deterministic cross-check no longer flags the metarepo "
        "divergence — the flag rate has gone to zero, which is a defect (#444 ac9)."
    )


def test_canary_completeness_flags_omitted_row():
    """A plan the checker returns NO verdict for a seed row on must still flag that
    row (completeness) — a silent omission cannot pass."""
    rows = list(SEED_ROWS)
    flags = flags_from_verdicts([], rows)  # checker returned nothing
    assert flags, "CANARY TRIPPED: an all-omitted verdict set produced zero flags."
    assert {f.axis for f in flags} >= {r.axis for r in SEED_ROWS}, (
        "CANARY TRIPPED: not every un-dispositioned seed row was flagged."
    )
