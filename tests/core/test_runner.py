"""Tests for the unattended-runner integration core (`run_once` + `checkpoint_bail`).

The runner is deliberately I/O-free at its edges: everything impure (the run-state
claim/log store, the base-prompt builder over spec_dispatch/dispatch, the per-item
git facts, and the "mark blocked" mutation) is injected via ``RunDeps`` so these
tests drive real orchestration with fakes — no agents, no git, no filesystem. The
two *pure* pieces (``select_runnable`` from Task 3 and ``resumable_dispatch`` from
Task 6) are exercised for real, since they need no faking. #unattended-runner
"""
from datetime import datetime, timezone

import pytest

from mship.core.run_state import ClaimInfo
from mship.core.runner import BranchState, RunDeps, checkpoint_bail, run_once

NOW = datetime(2026, 7, 8, tzinfo=timezone.utc)


def _wi(id, unattended=True, phase="ready", spec="s", created=NOW):
    from mship.core.workitem import WorkItem

    return WorkItem(id=id, title=id, workspace="ws", kind="feature",
                    created_at=created, updated_at=created, spec_id=spec,
                    unattended=unattended, phase_override=phase)


class FakeRunState:
    """Records claim/release/log calls; ``held`` seeds foreign holders so
    ``try_claim`` returns their ``ClaimInfo`` (caller must stand down)."""

    def __init__(self, held=None):
        self._held = dict(held or {})       # item_id -> holder currently holding it
        self.claims: list[tuple[str, str]] = []
        self.releases: list[tuple[str, str]] = []
        self.logs: list[tuple[str, str]] = []
        self.blocked: list[tuple[str, str]] = []   # spy for the injected mark_blocked
        self.pushed: list[str] = []                # spy for the injected push_branch

    def try_claim(self, item_id, holder, now):
        if item_id in self._held:
            return ClaimInfo(holder=self._held[item_id], heartbeat_at=now)
        self._held[item_id] = holder
        self.claims.append((item_id, holder))
        return None

    def read_claim(self, item_id):
        holder = self._held.get(item_id)
        return None if holder is None else ClaimInfo(holder=holder, heartbeat_at=NOW)

    def release(self, item_id, holder):
        if self._held.get(item_id) == holder:
            self._held.pop(item_id, None)
        self.releases.append((item_id, holder))

    def append_log(self, item_id, text, now):
        self.logs.append((item_id, text))


def _deps(items, *, spec_approved=None, held=None, commits_ahead=0,
          recent_journal=None, claimed=None, blocked=None, holder="runX"):
    rs = FakeRunState(held=held)
    return RunDeps(
        items=items,
        spec_approved=spec_approved if spec_approved is not None else {"s": True},
        claimed=claimed if claimed is not None else set(),
        blocked=blocked if blocked is not None else set(),
        run_state=rs,
        build_base_prompt=lambda it: f"BASE:{it.id}",
        branch_state=lambda it: BranchState(
            branch=f"feat/{it.id}",
            commits_ahead=commits_ahead,
            recent_journal=recent_journal or [],
        ),
        mark_blocked=lambda it, reason: rs.blocked.append((it.id, reason)),
        push_branch=lambda it: rs.pushed.append(it.id),
        holder=holder,
        now=lambda: NOW,
    )


@pytest.fixture
def fake_ctx():
    return _deps([_wi("wi-1")])


def test_run_once_claims_and_returns_prompt(fake_ctx):
    result = run_once(fake_ctx)
    assert result is not None
    assert result.item.id == "wi-1"
    assert result.holder == "runX"
    assert result.prompt == "BASE:wi-1"                 # fresh: no RESUMING preamble
    assert ("wi-1", "runX") in fake_ctx.run_state.claims  # claim was taken
    # the branch reference is recorded on the run-log at claim time
    assert any(item == "wi-1" and "feat/wi-1" in text
               for item, text in fake_ctx.run_state.logs)


def test_run_once_noop_when_nothing_eligible():
    # spec unapproved => selector yields nothing => no claim, no prompt
    deps = _deps([_wi("wi-1")], spec_approved={"s": False})
    assert run_once(deps) is None
    assert deps.run_state.claims == []


def test_run_once_skips_blocked_item():
    # A previously-bailed item (blocked) must not be re-picked → no claim. FIX#1
    deps = _deps([_wi("wi-1")], blocked={"wi-1"})
    assert run_once(deps) is None
    assert deps.run_state.claims == []


def test_bail_releases_claim_and_logs_reason(fake_ctx):
    run_once(fake_ctx)                                  # claim wi-1 first
    item = fake_ctx.items[0]
    checkpoint_bail(fake_ctx, item, "fork on auth approach")
    # release the claim
    assert ("wi-1", "runX") in fake_ctx.run_state.releases
    # log the reason
    assert any(i == "wi-1" and "fork on auth approach" in t
               for i, t in fake_ctx.run_state.logs)
    # mark the item blocked (via injected seam)
    assert ("wi-1", "fork on auth approach") in fake_ctx.run_state.blocked
    # branch reference is still recorded (claim-time log survives the bail)
    assert any(i == "wi-1" and "feat/wi-1" in t for i, t in fake_ctx.run_state.logs)


def test_bail_releases_cross_process_claim():
    # FIX#2: run-next claimed under another process's holder (runA); bail runs as a
    # different process (runB). Release must clear the claim by reading the RECORDED
    # holder off the ref, not this process's fresh token — otherwise it no-ops.
    deps = _deps([_wi("wi-1")], held={"wi-1": "runA"}, holder="runB")
    item = deps.items[0]
    checkpoint_bail(deps, item, "cross-process bail")
    assert ("wi-1", "runA") in deps.run_state.releases   # released the recorded holder
    assert "wi-1" not in deps.run_state._held            # claim actually cleared


def test_bail_pushes_branch_for_resume(fake_ctx):
    # FIX#4a: the branch must be pushed on bail so a later (fresh-clone) run resumes.
    run_once(fake_ctx)
    item = fake_ctx.items[0]
    checkpoint_bail(fake_ctx, item, "fork")
    assert "wi-1" in fake_ctx.run_state.pushed


def test_run_once_skips_item_already_claimed():
    # wi-old is held by another run; run_once must stand down and claim wi-new.
    old = _wi("wi-old", created=datetime(2026, 7, 8, 1, tzinfo=timezone.utc))
    new = _wi("wi-new", created=datetime(2026, 7, 8, 2, tzinfo=timezone.utc))
    deps = _deps([new, old], held={"wi-old": "otherRun"})
    result = run_once(deps)
    assert result is not None and result.item.id == "wi-new"   # skipped the held one
    assert ("wi-new", "runX") in deps.run_state.claims
    assert ("wi-old", "runX") not in deps.run_state.claims     # never claimed by us


def test_run_once_wraps_resuming_prompt_for_prior_work():
    # commits_ahead > 0 => resumable_dispatch (Task 6) prepends a RESUMING preamble.
    deps = _deps([_wi("wi-1")], commits_ahead=3, recent_journal=["wrote parser"])
    result = run_once(deps)
    assert result is not None
    assert "RESUMING" in result.prompt and "feat/wi-1" in result.prompt
    assert "3 commit" in result.prompt and "wrote parser" in result.prompt
    assert "BASE:wi-1" in result.prompt                 # base prompt still embedded
