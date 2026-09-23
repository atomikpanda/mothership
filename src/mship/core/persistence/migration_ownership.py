"""Read-only ownership reconciliation for the legacy-to-SQLite cutover."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import yaml
from pydantic import ValidationError

from mship.core.state import WorkspaceState
from mship.core.workitem import WorkItem


@dataclass(frozen=True)
class OwnershipResolution:
    task_slug: str
    owner_id: str
    rule: str
    evidence: tuple[str, ...]
    removed_owner_ids: tuple[str, ...]


@dataclass(frozen=True)
class OwnershipConflict:
    task_slug: str
    owner_ids: tuple[str, ...]
    reason: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class OwnershipChange:
    work_item_id: str
    before: tuple[str, ...]
    after: tuple[str, ...]


@dataclass(frozen=True)
class TaskOwnershipChange:
    task_slug: str
    before: None
    after: str


@dataclass(frozen=True)
class OwnershipPlan:
    resolutions: tuple[OwnershipResolution, ...] = ()
    conflicts: tuple[OwnershipConflict, ...] = ()
    changes: tuple[OwnershipChange, ...] = ()
    evidence_fingerprints: dict[str, str] = field(default_factory=dict)
    task_changes: tuple[TaskOwnershipChange, ...] = ()


def _historical_owners(
    state_dir: Path, slugs: set[str]
) -> tuple[dict[str, list[tuple[str, str]]], list[str], dict[str, str]]:
    """Use only application-native, retained WorkspaceState snapshots.

    A snapshot timestamp is not proof that an owner superseded another one.
    Every observed explicit owner must agree; an unreadable snapshot prevents
    automatic resolution because it could contain contradictory evidence.
    """
    owners: dict[str, list[tuple[str, str]]] = {}
    problems: list[str] = []
    fingerprints: dict[str, str] = {}
    if not slugs:
        return owners, problems, fingerprints
    root = state_dir.resolve()
    paths = sorted(
        [
            *(state_dir / "backups").glob("*/state.yaml"),
            *state_dir.glob("state.yaml.migrated-*"),
        ]
    )
    for path in paths:
        source = path.relative_to(state_dir).as_posix()
        try:
            if not path.resolve().is_relative_to(root):
                raise ValueError("historical snapshot escapes the state directory")
            payload = path.read_bytes()
            fingerprints[source] = sha256(payload).hexdigest()
            raw = yaml.safe_load(payload)
            snapshot = WorkspaceState.model_validate({} if raw is None else raw)
            for slug in sorted(slugs & snapshot.tasks.keys()):
                task = snapshot.tasks[slug]
                if task.slug != slug:
                    raise ValueError("historical Task key does not match its slug")
                if task.work_item_id is not None:
                    owners.setdefault(slug, []).append((task.work_item_id, source))
        except (OSError, ValueError, yaml.YAMLError, ValidationError) as error:
            problems.append(f"{source}: {type(error).__name__}")
    return owners, problems, fingerprints


def plan_ownership(
    state_dir: Path,
    state: WorkspaceState,
    items: list[WorkItem],
    *,
    owner_resolutions: Mapping[str, str] | None = None,
) -> OwnershipPlan:
    """Plan backreference corrections and missing Task owners without mutating inputs."""
    overrides = dict(owner_resolutions or {})
    if any(
        not isinstance(slug, str)
        or not slug.strip()
        or not isinstance(owner, str)
        or not owner.strip()
        for slug, owner in overrides.items()
    ):
        raise ValueError(
            "owner resolutions must map nonempty task slugs to WorkItem IDs"
        )
    by_id = {item.id: item for item in items}
    if len(by_id) != len(items):
        raise ValueError("legacy WorkItem IDs must be unique")
    claimants: dict[str, set[str]] = {}
    for item in items:
        for slug in item.task_slugs:
            claimants.setdefault(slug, set()).add(item.id)
    historical_slugs = {
        slug
        for slug, owners in claimants.items()
        if len(owners) > 1 and slug not in state.tasks and slug not in overrides
    }
    history, history_errors, fingerprints = _historical_owners(
        Path(state_dir), historical_slugs
    )
    resolutions: list[OwnershipResolution] = []
    conflicts: list[OwnershipConflict] = []
    task_changes: list[TaskOwnershipChange] = []
    chosen: dict[str, str] = {}
    for slug in sorted(claimants.keys() | state.tasks.keys() | overrides.keys()):
        owners = tuple(sorted(claimants.get(slug, ())))
        task = state.tasks.get(slug)
        explicit = task.work_item_id if task is not None else None
        override = overrides.get(slug)
        owner: str | None = None
        rule = ""
        evidence: tuple[str, ...] = ()
        reason = ""
        if task is not None and task.slug != slug:
            reason = "current Task key does not match its slug"
        elif explicit is not None and explicit not in by_id:
            reason = f"current Task references missing WorkItem {explicit!r}"
            evidence = (f"state.yaml:tasks.{slug}.work_item_id={explicit}",)
        elif explicit is not None:
            evidence = (f"state.yaml:tasks.{slug}.work_item_id={explicit}",)
            if override is not None and override != explicit:
                reason = (
                    "operator mapping contradicts the current Task's explicit owner"
                )
            else:
                owner, rule = explicit, "current-task"
        elif override is not None:
            evidence = (f"operator mapping: {slug}={override}",)
            if slug not in claimants:
                reason = "operator mapping has no existing task association to resolve"
            elif override not in by_id:
                reason = f"operator mapping references missing WorkItem {override!r}"
            elif override not in owners:
                reason = "operator mapping must select an existing claimant"
            else:
                owner, rule = override, "operator"
        elif len(owners) == 1:
            owner, rule = owners[0], "sole-claimant"
            evidence = (f"workitems/{owner}.json:task_slugs",)
        elif len(owners) > 1:
            observations = history.get(slug, [])
            observed = {item_id for item_id, _ in observations}
            evidence = tuple(
                f"{source}:tasks.{slug}.work_item_id={item_id}"
                for item_id, source in observations
            )
            if task is not None:
                reason = (
                    "current Task has no explicit owner and multiple WorkItems claim it"
                )
            elif history_errors:
                reason = "historical ownership evidence is unreadable or invalid"
                evidence += tuple(history_errors)
            elif len(observed) > 1:
                reason = "historical Task snapshots contradict one another"
            elif not observed:
                reason = "multiple WorkItems claim a removed task without durable ownership evidence"
            else:
                historical_owner = next(iter(observed))
                if historical_owner not in by_id:
                    reason = f"historical Task references missing WorkItem {historical_owner!r}"
                elif historical_owner not in owners:
                    reason = "historical owner is not a current claimant"
                else:
                    owner, rule = historical_owner, "historical-consensus"
        if reason:
            conflicts.append(OwnershipConflict(slug, owners, reason, evidence))
        elif owner is not None:
            chosen[slug] = owner
            if task is not None and explicit is None:
                task_changes.append(TaskOwnershipChange(slug, None, owner))
            resolutions.append(
                OwnershipResolution(
                    slug,
                    owner,
                    rule,
                    evidence,
                    tuple(item_id for item_id in owners if item_id != owner),
                )
            )
    changes = []
    for item in sorted(items, key=lambda item: item.id):
        before = tuple(item.task_slugs)
        after = tuple(
            dict.fromkeys(
                slug for slug in before if slug not in chosen or chosen[slug] == item.id
            )
        )
        if before != after:
            changes.append(OwnershipChange(item.id, before, after))
    return OwnershipPlan(
        resolutions=tuple(resolutions),
        conflicts=tuple(conflicts),
        changes=tuple(changes),
        evidence_fingerprints=fingerprints,
        task_changes=tuple(task_changes),
    )


def apply_ownership_plan(
    state: WorkspaceState, items: list[WorkItem], plan: OwnershipPlan
) -> tuple[WorkspaceState, list[WorkItem]]:
    """Apply a complete plan in memory, preserving every unrelated field."""
    if plan.conflicts:
        raise ValueError("cannot apply an ownership plan with unresolved conflicts")
    normalized_state = state
    if plan.task_changes:
        tasks = dict(state.tasks)
        for change in plan.task_changes:
            task = tasks.get(change.task_slug)
            if task is None or task.work_item_id is not None:
                raise ValueError(
                    "ownership plan no longer matches the legacy Task owner"
                )
            tasks[change.task_slug] = task.model_copy(
                update={"work_item_id": change.after}
            )
        normalized_state = state.model_copy(update={"tasks": tasks})
    changes = {change.work_item_id: change for change in plan.changes}
    normalized = []
    for item in items:
        change = changes.get(item.id)
        if change is None:
            normalized.append(item)
        else:
            if tuple(item.task_slugs) != change.before:
                raise ValueError(
                    "ownership plan no longer matches the legacy task links"
                )
            normalized.append(
                item.model_copy(update={"task_slugs": list(change.after)})
            )
    return normalized_state, normalized
