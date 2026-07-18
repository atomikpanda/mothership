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
import shlex
from dataclasses import dataclass

# `\b` word boundaries make `ac7` match the standalone token `ac7` but never the
# longer id `ac70` (`\d+` is greedy up to a boundary) nor an `ac7` buried inside
# another word (e.g. `mac7book`). Case-insensitive so `AC7` in a subject matches.
_AC_TOKEN_RE = re.compile(r"\bac\d+\b", re.IGNORECASE)

# Record/field separators for the `git log` format below. These control chars
# never appear in shas or human commit prose, so they delimit multi-line commit
# bodies unambiguously.
_FIELD_SEP = "\x1f"   # US -- between sha and message within one commit record
_COMMIT_SEP = "\x1e"  # RS -- between commit records


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
    """Plan the evidence links to add for `spec` (pure -- no mutation, no I/O).

    - every ref in `test_run_refs` -> a `test` link on EVERY acceptance criterion;
    - every `(sha, message)` in `commits` -> a `commit` link on each acceptance
      criterion whose id is named (word-boundary) in the message.

    De-duplicated against the spec's existing evidence and within the batch, so
    the result never repeats an existing `(criterion, kind, ref)`. This is what
    makes finish idempotent (ac6) and additive to manual evidence (ac7)."""
    id_by_lower = {c.id.lower(): c.id for c in spec.acceptance_criteria}
    existing: set[tuple[str, str, str]] = {
        (c.id, e.kind, e.ref)
        for c in spec.acceptance_criteria
        for e in c.evidence
    }
    seen: set[tuple[str, str, str]] = set()
    links: list[EvidenceLink] = []

    def _add(criterion_id: str, kind: str, ref: str) -> None:
        key = (criterion_id, kind, ref)
        if key in existing or key in seen:
            return
        seen.add(key)
        links.append(EvidenceLink(criterion_id=criterion_id, kind=kind, ref=ref))

    for ref in test_run_refs:
        for c in spec.acceptance_criteria:
            _add(c.id, "test", ref)
    for sha, message in commits:
        for token in extract_ac_ids(message):
            criterion_id = id_by_lower.get(token)
            if criterion_id is not None:
                _add(criterion_id, "commit", sha)
    return links


def passing_test_run_refs(task) -> list[str]:
    """Return `test-runs/<iteration>.<repo>` refs for every repo whose most recent
    recorded test result passed (spec 377 ac10). `task.test_iteration` is the run
    number; `task.test_results[repo].status == "pass"` selects the repos. Empty
    when the task has never recorded a test iteration."""
    iteration = getattr(task, "test_iteration", 0) or 0
    if iteration <= 0:
        return []
    refs: list[str] = []
    for repo in sorted(task.test_results):
        result = task.test_results[repo]
        if getattr(result, "status", None) == "pass":
            refs.append(f"test-runs/{iteration}.{repo}")
    return refs


def commits_since_base(shell, repo_path, base, branch) -> list[tuple[str, str]]:
    """Return `(sha, message)` for each commit on `branch` since `origin/<base>`
    (git log default order). Mirrors the `origin/<base>..<branch>` range that
    `mship finish` already uses for its subject scan. Returns `[]` on any git
    failure (fail-open: a missing branch simply yields no commit evidence)."""
    eff_base = base or "HEAD"
    rng = f"origin/{eff_base}..{branch}"
    result = shell.run(
        f"git log --format=%H{_FIELD_SEP}%B{_COMMIT_SEP} {shlex.quote(rng)}",
        cwd=repo_path,
    )
    if result.returncode != 0:
        return []
    commits: list[tuple[str, str]] = []
    for record in result.stdout.split(_COMMIT_SEP):
        record = record.strip()
        if not record:
            continue
        sha, _, message = record.partition(_FIELD_SEP)
        sha = sha.strip()
        if sha:
            commits.append((sha, message.strip()))
    return commits
