"""Wraps a dispatch prompt so a resumed unattended run continues prior work.

If the task's branch already has commits ahead of base, prepend a "RESUMING"
preamble stating the branch, its commits-ahead count, and the last journal
entries, instructing the agent to continue rather than restart. Pure — no I/O;
callers supply the branch/commit/journal facts they've already loaded.
#unattended-runner
"""
from __future__ import annotations


def resumable_dispatch(*, base_prompt, branch, commits_ahead, recent_journal):
    if commits_ahead <= 0:
        return base_prompt                      # fresh start: no preamble
    tail = "\n".join(f"- {j}" for j in recent_journal[-5:])
    return (f"## RESUMING prior run\n"
            f"You are continuing WorkItem work already in progress on branch "
            f"`{branch}` ({commits_ahead} commit(s) ahead of base). Do NOT restart — "
            f"build on the existing commits. Recent journal:\n{tail}\n\n{base_prompt}")
