"""Injected terminal selection for profile target ambiguity."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from mship.cli.output import Output
from mship.core.run_target.models import (
    SelectedTarget,
    TargetSelectionError,
    safe_identifier,
)

if TYPE_CHECKING:
    from mship.core.run_target.models import AppRun


def _can_prompt(*, interactive: bool, output: Output) -> bool:
    return interactive and output.is_tty and output.human_mode


def _select_number(
    count: int,
    *,
    kind: str,
    interactive: bool,
    input_fn: Callable[[str], str],
    output: Output,
) -> int:
    if kind == "profile":
        option = "--profile"
        code = "profile_missing"
    elif kind == "run":
        option = "--run-id"
        code = "context_ambiguous"
    else:
        option = "--host <name> or --target <friendly-alias>"
        code = "target_ambiguous"
    if not _can_prompt(interactive=interactive, output=output):
        raise TargetSelectionError(
            code,
            f"multiple {kind} choices; rerun with {option}",
        )
    while True:
        output.progress(f"Select {kind} [1-{count}, cancel]:")
        answer = input_fn("").strip()
        if answer.lower() in {"cancel", "c", "q", "quit"}:
            raise TargetSelectionError(
                f"{kind}_selection_cancelled", f"{kind} selection cancelled"
            )
        try:
            index = int(answer)
        except ValueError:
            output.error(f"enter a number from 1 to {count}, or cancel")
            continue
        if 1 <= index <= count:
            return index - 1
        output.error(f"enter a number from 1 to {count}, or cancel")


def choose_target(
    candidates: Sequence[SelectedTarget],
    *,
    profile_name: str,
    backend_name: str,
    interactive: bool,
    input_fn: Callable[[str], str],
    output: Output,
) -> SelectedTarget:
    """Choose a safe friendly target, or fail without touching stdin."""
    safe_identifier(profile_name, field="profile name")
    safe_identifier(backend_name, field="backend name")
    if not candidates:
        raise TargetSelectionError("target_unavailable", "No eligible target")
    if len(candidates) == 1:
        return candidates[0]
    if _can_prompt(interactive=interactive, output=output):
        output.print("Select a target:")
        for number, selected in enumerate(candidates, start=1):
            candidate = selected.candidate
            aliases = ", ".join(candidate.aliases) or "none"
            readiness = (
                "ready" if candidate.ready else candidate.reason or "unavailable"
            )
            output.print(
                f"{number}. host: {selected.host.name}; profile: {profile_name}; backend: {backend_name}; "
                f"target: {candidate.label}; aliases: {aliases}; readiness: {readiness}; "
                f"scope: {selected.host.scope}"
            )
    index = _select_number(
        len(candidates),
        kind="target",
        interactive=interactive,
        input_fn=input_fn,
        output=output,
    )
    return candidates[index]


def choose_run(
    candidates: Sequence[AppRun],
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
    output: Output,
) -> AppRun:
    """Choose an acknowledged run using only its public-safe identity fields."""
    if not candidates:
        raise TargetSelectionError("context_ambiguous", "no matching active run")
    if len(candidates) == 1:
        return candidates[0]
    if _can_prompt(interactive=interactive, output=output):
        output.print("Select a recorded run:")
        for number, run in enumerate(candidates, start=1):
            output.print(
                f"{number}. run: {run.id}; repo: {run.repo}; profile: {run.profile}; "
                f"host: {run.host_name}; target: {run.safe_target_label}; status: {run.status}"
            )
    index = _select_number(
        len(candidates),
        kind="run",
        interactive=interactive,
        input_fn=input_fn,
        output=output,
    )
    return candidates[index]


def choose_profile(
    names: Sequence[str],
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
    output: Output,
) -> str:
    """Choose one configured profile by typed friendly name, never a target."""
    if not names:
        raise TargetSelectionError("profile_missing", "no run profile is configured")
    if len(set(names)) != len(names):
        raise TargetSelectionError(
            "constraint_conflict", "configured profile names are not unique"
        )
    if len(names) == 1 and _can_prompt(interactive=interactive, output=output):
        return names[0]
    if _can_prompt(interactive=interactive, output=output):
        output.print("Select a run profile:")
        for number, name in enumerate(names, start=1):
            output.print(f"{number}. {name}")
    return names[
        _select_number(
            len(names),
            kind="profile",
            interactive=interactive,
            input_fn=input_fn,
            output=output,
        )
    ]
