"""Promote a capture into acceptance-criterion evidence.

Bare `mship capture` is untouched: it writes to the ephemeral, gitignored
captures directory and nothing here runs. Only `--evidence` promotes artifacts
into the durable, spec-scoped, mode-governed store.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EvidenceTarget:
    spec_id: str
    criterion_id: str


def parse_evidence_target(raw: str) -> EvidenceTarget:
    """`<spec-id>:<ac-id>` -> EvidenceTarget. Raises ValueError otherwise."""
    parts = (raw or "").split(":")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"--evidence expects <spec-id>:<criterion-id> (e.g. my-spec:ac3), got {raw!r}"
        )
    return EvidenceTarget(spec_id=parts[0], criterion_id=parts[1])


def _on_a_branch(sha: str, worktree: Path, shell) -> bool:
    """True iff some branch — local or remote-tracking — contains `sha`.

    `git branch --all --contains <sha>` lists every branch reachable from
    `sha`, but on a detached HEAD it ALSO always emits a synthetic
    `* (HEAD detached at/from ...)` line even when no real branch contains the
    commit — so "empty output" isn't the right test. Filter that pseudo-entry
    out (it starts with `(`) and check whether any real branch name remains.
    `--all` (not just local) matters because a worktree can be a detached
    checkout of a commit that's the tip of a REMOTE branch with no local
    branch pointing at it — that commit is still "on a branch", just not one
    with a local ref.
    """
    result = shell.run(f"git branch --all --contains {sha}", cwd=worktree)
    for line in (result.stdout or "").splitlines():
        name = line[2:].strip()  # strip the leading "* " / "  " marker column
        if name and not name.startswith("("):
            return True
    return False


def provenance_note(worktree: Path, shell) -> str:
    """Where the capture was taken from. A capture of uncommitted work, or of
    a commit that isn't on any branch (a detached HEAD, or a throwaway run
    ref materialized for a remote capture), is still useful evidence — but a
    reviewer must be able to see that is what it is.

    `shell` is util/shell.py::Shell — its `run` takes a command STRING (it uses
    shell=True), not an argv list.
    """
    rev = shell.run("git rev-parse --short HEAD", cwd=worktree)
    sha = (rev.stdout or "").strip() or "unknown"
    status = shell.run("git status --porcelain", cwd=worktree)
    dirty = bool((status.stdout or "").strip())

    markers = []
    if dirty:
        markers.append("uncommitted working tree")
    if sha != "unknown" and not _on_a_branch(sha, worktree, shell):
        markers.append("not on any branch")

    if markers:
        return f"at {sha} ({', '.join(markers)})"
    return f"at {sha}"
