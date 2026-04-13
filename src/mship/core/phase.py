from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from mship.core.log import LogManager
from mship.core.state import StateManager

Phase = Literal["plan", "dev", "review", "run"]
PHASE_ORDER: list[Phase] = ["plan", "dev", "review", "run"]


class FinishedTaskError(RuntimeError):
    """Raised when transitioning a finished task to plan/dev/review without --force."""


@dataclass
class PhaseTransition:
    new_phase: Phase
    warnings: list[str] = field(default_factory=list)


class PhaseManager:
    """Manages phase transitions with soft gates."""

    def __init__(self, state_manager: StateManager, log: LogManager) -> None:
        self._state_manager = state_manager
        self._log = log

    def transition(
        self,
        task_slug: str,
        target: Phase,
        force_unblock: bool = False,
        force_finished: bool = False,
    ) -> PhaseTransition:
        state = self._state_manager.load()
        task = state.tasks[task_slug]

        # Finished-task guardrail: plan/dev/review refuse; run is always allowed.
        if task.finished_at is not None and target != "run" and not force_finished:
            raise FinishedTaskError(
                f"Task '{task_slug}' is finished. Transitioning to {target} "
                f"probably means you want `mship close` then `mship spawn` for "
                f"the next task. Use --force to override."
            )

        old_phase = task.phase
        warnings = self._check_gates(task_slug, task.phase, target)

        # Clear blocked state only if force_unblock is set
        if task.blocked_reason is not None and force_unblock:
            warnings.append(
                f"Task was blocked: {task.blocked_reason} — force-unblocked by phase transition"
            )
            self._log.append(
                task_slug,
                f"Unblocked (forced phase transition to {target})",
            )
            task.blocked_reason = None
            task.blocked_at = None

        if task.finished_at is not None and force_finished and target != "run":
            warnings.append(
                f"Task was finished (at {task.finished_at.isoformat()}) — "
                f"forced transition to {target}"
            )

        task.phase = target
        task.phase_entered_at = datetime.now(timezone.utc)
        self._state_manager.save(state)

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
        return ["No spec found — consider writing one before developing"]

    def _gate_review(self, task) -> list[str]:
        warnings: list[str] = []
        missing = []
        failing = []
        for repo in task.affected_repos:
            result = task.test_results.get(repo)
            if result is None:
                missing.append(repo)
            elif result.status == "fail":
                failing.append(repo)

        if missing:
            warnings.append(
                f"Tests not run in: {', '.join(missing)} — consider running tests before review"
            )
        if failing:
            warnings.append(
                f"Tests not passing in: {', '.join(failing)} — consider fixing before review"
            )
        return warnings

    def _gate_run(self, task) -> list[str]:
        return []
