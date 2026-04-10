from dataclasses import dataclass, field
from typing import Literal

from mship.core.state import StateManager

Phase = Literal["plan", "dev", "review", "run"]
PHASE_ORDER: list[Phase] = ["plan", "dev", "review", "run"]


@dataclass
class PhaseTransition:
    new_phase: Phase
    warnings: list[str] = field(default_factory=list)


class PhaseManager:
    """Manages phase transitions with soft gates."""

    def __init__(self, state_manager: StateManager) -> None:
        self._state_manager = state_manager

    def transition(self, task_slug: str, target: Phase) -> PhaseTransition:
        state = self._state_manager.load()
        task = state.tasks[task_slug]
        warnings = self._check_gates(task_slug, task.phase, target)

        task.phase = target
        self._state_manager.save(state)

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
