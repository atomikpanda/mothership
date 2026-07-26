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


def provenance_note(worktree: Path, shell) -> str:
    """Where the capture was taken from. A capture of uncommitted work or of a
    throwaway run ref is still useful evidence, but a reviewer must be able to
    see that is what it is.

    `shell` is util/shell.py::Shell — its `run` takes a command STRING (it uses
    shell=True), not an argv list.
    """
    rev = shell.run("git rev-parse --short HEAD", cwd=worktree)
    sha = (rev.stdout or "").strip() or "unknown"
    status = shell.run("git status --porcelain", cwd=worktree)
    dirty = bool((status.stdout or "").strip())
    return f"at {sha} (uncommitted working tree)" if dirty else f"at {sha}"
