"""Integration core for the unattended (cloud) runner: one selection→claim→dispatch
tick, plus a checkpoint bail.

This is the seam the CLI/serve host calls. It composes the pieces landed earlier on
this branch:

* Task 3 — ``run_select.select_runnable`` picks the eligible, oldest-first backlog.
* Task 5 — ``run_state.RunStateRepo`` (duck-typed here as ``run_state``) is the
  git-backed claim + run-log: ``try_claim`` returns ``None`` when we win the claim or
  a ``ClaimInfo`` when another run already holds it; ``release`` and ``append_log``
  round it out.
* Task 6 — ``run_dispatch.resumable_dispatch`` wraps the base prompt with a
  "RESUMING" preamble when the item's branch already has commits, so a resumed run
  continues instead of restarting.
* the existing ``spec_dispatch``/``dispatch`` path builds the base prompt (injected
  as ``build_base_prompt`` so this module never spawns agents or shells git).

Everything impure is injected through ``RunDeps`` so the orchestration is unit-tested
with fakes — no agents, no git, no filesystem. The two *pure* dependencies
(``select_runnable`` and ``resumable_dispatch``) are called directly; only the
impure edges (claim/log store, base-prompt builder, per-item git facts, and the
"mark blocked" mutation) are seams.

``run_once`` never merges and never opens a PR — it only returns the prompt for the
host to execute. When the host phase throws, the host calls ``checkpoint_bail`` to
record the reason, mark the item blocked, and release the claim so the backlog can
move on (and so a later run can resume the branch). #unattended-runner
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from mship.core.run_dispatch import resumable_dispatch
from mship.core.run_select import select_runnable


@dataclass(frozen=True)
class BranchState:
    """Per-item git facts the resumable wrapper needs, gathered by the host.

    ``commits_ahead <= 0`` means a fresh start (no preamble); otherwise the prompt
    is wrapped so the agent continues the existing branch."""

    branch: str
    commits_ahead: int
    recent_journal: list[str] = field(default_factory=list)


@dataclass
class RunDeps:
    """Injectable seams for one runner tick.

    Data snapshots (``items``/``spec_approved``/``claimed``) are what the pure Task 3
    selector consumes; the callables and ``run_state`` are the impure edges a real
    host wires to ``WorkItemStore``, the spec store, ``RunStateRepo``, git, and the
    ``spec_dispatch``/``dispatch`` prompt builder.
    """

    items: list                                     # loaded WorkItems (this tick's snapshot)
    spec_approved: dict[str, bool]                  # spec_id -> is approved
    claimed: set[str]                               # item ids known-claimed (best-effort snapshot)
    run_state: object                               # RunStateRepo-like: try_claim/read_claim/release/append_log
    build_base_prompt: Callable[[object], str]      # item -> base dispatch prompt (spec_dispatch/dispatch)
    branch_state: Callable[[object], BranchState]   # item -> its branch facts for the resumable wrap
    mark_blocked: Callable[[object, str], None]     # (item, reason) -> mark it blocked (host decides how)
    push_branch: Callable[[object], None]           # item -> push its task branch to origin (bail: survive for resume)
    holder: str                                     # opaque run token used for the claim
    now: Callable[[], datetime]                     # injected clock
    blocked: set[str] = field(default_factory=set)  # item ids with a bailed/blocked task (FIX#1 exclusion)


@dataclass(frozen=True)
class RunOnceResult:
    """The outcome of a claimed tick: the item we hold and the prompt to execute."""

    item: object                # the claimed WorkItem
    prompt: str                 # the (resumable) dispatch prompt for the host to run
    holder: str                 # the run token that holds the claim


def run_once(deps: RunDeps) -> RunOnceResult | None:
    """Select the next runnable WorkItem, claim it, and return its dispatch prompt.

    Returns ``None`` when nothing is eligible or every candidate is already held by
    another run (a no-op tick). Otherwise the returned claim is held by
    ``deps.holder``; on success or failure of the *host* phase the caller must call
    ``checkpoint_bail`` (or the run-state release) to give it back.

    Iterates candidates oldest-first and treats ``try_claim`` as the atomic gate:
    a ``ClaimInfo`` return means another run won the race, so we stand down and try
    the next candidate rather than error. Never merges.
    """
    candidates = select_runnable(deps.items, deps.spec_approved, deps.claimed, deps.blocked)
    for cand in candidates:
        item = cand.item
        existing = deps.run_state.try_claim(item.id, deps.holder, deps.now())
        if existing is not None:
            continue  # another run holds it → stand down, try the next candidate
        bs = deps.branch_state(item)
        prompt = resumable_dispatch(
            base_prompt=deps.build_base_prompt(item),
            branch=bs.branch,
            commits_ahead=bs.commits_ahead,
            recent_journal=bs.recent_journal,
        )
        # Record the branch reference on the run-log at claim time so a later
        # (possibly resuming) run — and a bail — can point back to this branch.
        deps.run_state.append_log(
            item.id,
            f"run claimed by {deps.holder} on branch {bs.branch} "
            f"({bs.commits_ahead} commit(s) ahead of base)",
            deps.now(),
        )
        return RunOnceResult(item=item, prompt=prompt, holder=deps.holder)
    return None


def checkpoint_bail(deps: RunDeps, item, reason: str) -> None:
    """Bail out of a claimed item: log the reason, push the branch, mark it blocked,
    release the claim.

    Called by the host when the run cannot make progress (an exception, a fork in the
    approach, a needed decision). Order matters:

    1. the reason is logged first so it is durable even if later steps race;
    2. the task branch is pushed to origin (best-effort) so the work survives for a
       later resume — even on an ephemeral host that will be torn down (AC6/FIX#4a);
    3. the item is marked blocked (so the selector won't re-pick it, FIX#1);
    4. the claim is released so the backlog can move on.

    Release is *authoritative*: run-next and bail are separate processes with
    different holder tokens, so releasing under ``deps.holder`` would no-op. We read
    the claim's RECORDED holder off the ref and release as that (FIX#2). Never merges
    — the branch is left intact for a later resume.
    """
    deps.run_state.append_log(item.id, f"bailed: {reason}", deps.now())
    try:
        deps.push_branch(item)  # best-effort: a push failure must not strand the bail
    except Exception:  # noqa: BLE001 — the block/release below must still run
        pass
    deps.mark_blocked(item, reason)
    claim = deps.run_state.read_claim(item.id)
    holder = claim.holder if claim is not None else deps.holder
    deps.run_state.release(item.id, holder)
