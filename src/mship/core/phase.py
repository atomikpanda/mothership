from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mship.core.log import LogManager
from mship.core.state import StateManager

if TYPE_CHECKING:
    from mship.core.config import WorkspaceConfig

Phase = Literal["plan", "dev", "review", "run"]
PHASE_ORDER: list[Phase] = ["plan", "dev", "review", "run"]


class FinishedTaskError(RuntimeError):
    """Raised when transitioning a finished task to plan/dev/review without --force."""


class SpecGateError(RuntimeError):
    """Raised when plan→dev requires an approved spec and none is bound to the task."""


@dataclass
class PhaseTransition:
    new_phase: Phase
    warnings: list[str] = field(default_factory=list)


class PhaseManager:
    """Manages phase transitions with soft gates."""

    def __init__(
        self,
        state_manager: StateManager,
        log: LogManager,
        config: "WorkspaceConfig | None" = None,
        workspace_root: Path | None = None,
    ) -> None:
        self._state_manager = state_manager
        self._log = log
        self._config = config
        self._workspace_root = workspace_root

    def transition(
        self,
        task_slug: str,
        target: Phase,
        force_unblock: bool = False,
        force_finished: bool = False,
        bypass_spec_gate: bool = False,
    ) -> PhaseTransition:
        # Read-only preflight: compute soft-gate warnings from current state.
        # These read repo state (specs, tests, uncommitted files) and mship
        # state; we recompute them outside the mutate lock so file I/O doesn't
        # happen under the exclusive state lock. The mutate() call below
        # re-reads the task to apply its mutations atomically.
        state = self._state_manager.load()
        task = state.tasks[task_slug]

        # Finished-task guardrail: plan/dev/review refuse; run is always allowed.
        if task.finished_at is not None and target != "run" and not force_finished:
            raise FinishedTaskError(
                f"Task '{task_slug}' is finished. Transitioning to {target} "
                f"probably means you want `mship close` then `mship spawn` for "
                f"the next task. Use --force to override."
            )

        # WorkItem gate: universal — every task entering dev must be linked to
        # a WorkItem, and a feature-kind WorkItem additionally needs an
        # approved spec (core/workitem_gate.py::check_task_gate, authoritative
        # for WorkItem-linked tasks). --bypass-spec-gate is the --hotfix
        # equivalent for this gate: it skips both checks below and instead
        # records a bypass-log entry. See spec
        # workitem-mandatory-kind-gated-approval.
        if task.phase == "plan" and target == "dev":
            if bypass_spec_gate:
                if self._workspace_root is not None:
                    from mship.core.workitem_gate import log_hotfix
                    log_hotfix(self._workspace_root, "phase-dev", task_slug)
            else:
                if self._workspace_root is not None:
                    from mship.core.workitem_gate import check_task_gate
                    gate_result = check_task_gate(task, self._workspace_root)
                    if not gate_result.ok:
                        raise SpecGateError(gate_result.reason)

                # Approved-spec gate: opt-in via require_approved_spec in
                # mothership.yaml. Older, task-slug-based and kind-agnostic —
                # kept as a fallback alongside the WorkItem-kind gate above,
                # which is authoritative for tasks that have a WorkItem.
                if (
                    self._config is not None
                    and self._config.require_approved_spec
                    and not self._has_approved_spec(task_slug)
                ):
                    raise SpecGateError(
                        f"Task '{task_slug}' has no bound approved spec. "
                        f"Create and approve one (`mship spec approve`) or pass "
                        f"--bypass-spec-gate to skip this check."
                    )

        old_phase = task.phase
        warnings = self._check_gates(task_slug, task.phase, target)

        finished_override = (
            task.finished_at is not None and force_finished and target != "run"
        )
        if finished_override:
            warnings.append(
                f"Task was finished (at {task.finished_at.isoformat()}) — "
                f"forced transition to {target}"
            )

        blocked_force_unblock = task.blocked_reason is not None and force_unblock
        if blocked_force_unblock:
            warnings.append(
                f"Task was blocked: {task.blocked_reason} — force-unblocked by phase transition"
            )

        def _apply(s):
            t = s.tasks[task_slug]
            if blocked_force_unblock:
                t.blocked_reason = None
                t.blocked_at = None
            t.phase = target
            t.phase_entered_at = datetime.now(timezone.utc)

        self._state_manager.mutate(_apply)

        # Journal entries happen outside the mutate — LogManager writes a
        # separate file, not mship state.
        if blocked_force_unblock:
            self._log.append(
                task_slug,
                f"Unblocked (forced phase transition to {target})",
            )
        self._log.append(task_slug, f"Phase transition: {old_phase} → {target}")

        return PhaseTransition(new_phase=target, warnings=warnings)

    def _check_gates(
        self, task_slug: str, current: Phase, target: Phase
    ) -> list[str]:
        current_idx = PHASE_ORDER.index(current)
        target_idx = PHASE_ORDER.index(target)

        if target_idx <= current_idx:
            return []

        warnings: list[str] = []
        state = self._state_manager.load()
        task = state.tasks[task_slug]

        if target == "dev":
            warnings.extend(self._gate_dev(task_slug))
        elif target == "review":
            warnings.extend(self._gate_review(task))
        elif target == "run":
            warnings.extend(self._gate_run(task))

        return warnings

    def _gate_dev(self, task_slug: str) -> list[str]:
        warn = (
            "No spec found — consider writing one before developing "
            "(create one with `mship spec new` or set `spec_paths` "
            "in mothership.yaml)"
        )
        # Without DI'd config + workspace_root we can't actually search for
        # specs — fall back to the always-warn stub. See #113 for the wiring.
        if self._config is None or self._workspace_root is None:
            return [warn]
        # Blessed task-scoped path (#126) is the primary check. If absent,
        # fall through to the workspace-level + worktree search so existing
        # workspaces (`docs/superpowers/specs/...`) still satisfy the gate.
        from mship.core.view.spec_discovery import (
            SpecNotFoundError, blessed_spec_path, find_spec,
        )
        if blessed_spec_path(self._workspace_root, task_slug).is_file():
            return []
        try:
            find_spec(
                self._workspace_root,
                None,
                state=self._state_manager.load(),
                spec_paths=self._config.spec_paths,
            )
        except SpecNotFoundError:
            return [warn]
        return []

    def _has_approved_spec(self, task_slug: str) -> bool:
        """Return True if a spec bound to task_slug has an approved-or-beyond status."""
        if self._workspace_root is None:
            return False
        # Import inside method to avoid potential import cycles at module load.
        from mship.core.spec_store import SPECS_DIRNAME, SpecStore

        specs_dir = self._workspace_root / SPECS_DIRNAME
        try:
            specs = SpecStore(specs_dir).list()
        except Exception:
            return False
        approved_statuses = {"approved", "dispatched", "implemented"}
        return any(
            s.task_slug == task_slug and s.status in approved_statuses
            for s in specs
        )

    def _gate_review(self, task) -> list[str]:
        # Unified reader honors both task.test_results and journal
        # `test_state=pass` entries so explicit evidence suppresses the
        # warning. See #81.
        from mship.core.test_evidence import format_missing_summary, read_evidence

        evidence = read_evidence(task, self._log)
        lines = format_missing_summary(evidence)
        if not lines:
            return []
        hint = " — consider running tests before review"
        return [lines[0] + hint] + lines[1:]

    def _gate_run(self, task) -> list[str]:
        return []
