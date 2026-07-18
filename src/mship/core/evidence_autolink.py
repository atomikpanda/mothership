"""Auto-link acceptance-criterion evidence at `mship finish` (spec 377).

For a spec-bound task, `mship finish` attaches:
  1. the task's passing test-run reference(s) (`test-runs/<iter>.<repo>`) to EVERY
     acceptance criterion, and
  2. each implementing commit's sha to the acceptance criterion/criteria whose id
     token (e.g. `ac7`) appears — on a word boundary — in the commit message.

`compute_evidence_links` is a pure planner: it never mutates the spec. It returns
the links to add, already de-duplicated against the spec's existing evidence, so
re-running finish is idempotent and manual `mship spec evidence` entries survive.
The finish command applies the links via `spec_review.set_criterion_evidence` and
persists with `SpecStore`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# `\b` word boundaries make `ac7` match the standalone token `ac7` but never the
# longer id `ac70` (`\d+` is greedy up to a boundary) nor an `ac7` buried inside
# another word (e.g. `mac7book`). Case-insensitive so `AC7` in a subject matches.
_AC_TOKEN_RE = re.compile(r"\bac\d+\b", re.IGNORECASE)


@dataclass(frozen=True)
class EvidenceLink:
    """One evidence entry to append to an acceptance criterion."""

    criterion_id: str
    kind: str  # "test" | "commit"
    ref: str


def extract_ac_ids(message: str) -> set[str]:
    """Return the lowercased `ac<number>` tokens named on word boundaries in
    `message` (e.g. `"fixes ac1 and AC3"` -> `{"ac1", "ac3"}`). Substring hits
    inside a larger word (`mac7book`) and the longer id `ac70` when only `ac7`
    is written are excluded by the word-boundary anchors."""
    return {m.group(0).lower() for m in _AC_TOKEN_RE.finditer(message or "")}


def compute_evidence_links(spec, commits, test_run_refs) -> list[EvidenceLink]:
    """Plan the evidence links to add for `spec` (pure -- no mutation).

    - every ref in `test_run_refs` -> a `test` link on EVERY acceptance criterion;
    - every `(sha, message)` in `commits` -> a `commit` link on each acceptance
      criterion whose id is named (word-boundary) in the message.

    (De-duplication is added in the next task.)"""
    id_by_lower = {c.id.lower(): c.id for c in spec.acceptance_criteria}
    links: list[EvidenceLink] = []
    for ref in test_run_refs:
        for c in spec.acceptance_criteria:
            links.append(EvidenceLink(criterion_id=c.id, kind="test", ref=ref))
    for sha, message in commits:
        for token in extract_ac_ids(message):
            criterion_id = id_by_lower.get(token)
            if criterion_id is not None:
                links.append(EvidenceLink(criterion_id=criterion_id, kind="commit", ref=sha))
    return links
