"""Pure host eligibility and deterministic profile-first target ranking."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from mship.core.run_host.config import HostRegistration
from mship.core.run_target.models import (
    HostInventory,
    HostRequirements,
    SelectedTarget,
    TargetSelectionError,
    safe_identifier,
)
from mship.core.run_target.preferences import TargetPreference


def eligible_hosts(
    hosts: Mapping[str, HostRegistration],
    *,
    allowed_roles: Sequence[str],
    role_hosts: Mapping[str, Sequence[str]],
    required: HostRequirements,
    host_name: str | None,
    remote_role: str | None,
) -> tuple[HostRegistration, ...]:
    """Return only hosts authorized by every explicit and project constraint.

    ``role_hosts`` is an authorization policy, not a ranking input: an omitted
    role allows its declared pool; an empty list authorizes no host for it.
    """
    if not required.roles:
        raise TargetSelectionError("constraint_conflict", "profile must request at least one host role")
    allowed = frozenset(allowed_roles)
    if host_name is not None:
        host = hosts.get(host_name)
        if host is None:
            raise TargetSelectionError("host_unknown", "requested host is not registered")
        candidates = (host,)
    else:
        candidates = tuple(hosts.values())

    def role_allowed(role: str, host: HostRegistration) -> bool:
        permitted = role_hosts.get(role)
        return permitted is None or host.name in permitted

    eligible: list[HostRegistration] = []
    for host in candidates:
        profile_roles = tuple(role for role in host.roles if role in allowed and role in required.roles)
        if not profile_roles or not any(role_allowed(role, host) for role in profile_roles):
            continue
        if not set(required.tags).issubset(host.tags):
            continue
        if remote_role is not None:
            if remote_role not in allowed or remote_role not in host.roles or not role_allowed(remote_role, host):
                continue
        eligible.append(host)

    if not eligible:
        raise TargetSelectionError("constraint_conflict", "no registered host satisfies the requested constraints")
    return tuple(eligible)


def _best_ranked(
    pairs: Sequence[SelectedTarget],
    *,
    preference: TargetPreference | None,
    preferred_role: str | None,
) -> tuple[SelectedTarget, ...]:
    if not pairs:
        raise TargetSelectionError("target_unavailable", "No eligible target")

    def remembered(pair: SelectedTarget) -> bool:
        return preference is not None and (
            preference.host_name is None or preference.host_name == pair.host.name
        ) and (
            preference.target_alias is None or preference.target_alias in pair.candidate.aliases
        )

    if preference is not None and not any(remembered(pair) for pair in pairs):
        raise TargetSelectionError(
            "target_preference_stale",
            "remembered target is unavailable; choose a target explicitly",
        )

    def score(pair: SelectedTarget) -> tuple[tuple[int, ...], bool, bool, int]:
        return (
            pair.candidate.rank,
            remembered(pair),
            preferred_role in pair.host.roles if preferred_role else False,
            pair.host.preference,
        )

    best = max(map(score, pairs))
    return tuple(pair for pair in pairs if score(pair) == best)


def rank_targets(
    inventories: Sequence[HostInventory],
    *,
    profile_revision: str,
    operation: str,
    preference: TargetPreference | None = None,
    preferred_role: str | None = None,
) -> tuple[SelectedTarget, ...]:
    """Return all equal winners after completeness and compatibility checks.

    Inventories are already scoped by :func:`eligible_hosts` and operation-
    specific discovery.  Callers pass ``preference=None`` whenever an explicit
    host or target constraint is present; explicit constraints are never
    weakened by remembered state. This function never uses labels, host
    enumeration, or backend keys to resolve a tie.
    """
    safe_identifier(operation, field="operation")
    if not inventories:
        raise TargetSelectionError("target_unavailable", "No eligible target")
    if any(inventory.profile_revision != profile_revision for inventory in inventories):
        raise TargetSelectionError("backend_protocol", "target inventory profile revision does not match the requested profile")
    if any(inventory.operation != operation for inventory in inventories):
        raise TargetSelectionError("backend_protocol", "target inventory operation does not match the requested operation")
    failures = tuple(
        f"{inventory.host.name}: {inventory.error}"
        for inventory in inventories
        if inventory.error is not None
    )
    if failures:
        raise TargetSelectionError(
            "discovery_incomplete",
            "target discovery did not complete for every eligible host",
            failures,
        )

    backend_revisions = {inventory.backend_revision for inventory in inventories}
    schemas = {inventory.rank_schema for inventory in inventories}
    if len(backend_revisions) != 1 or len(schemas) != 1:
        raise TargetSelectionError("backend_protocol", "target inventories are not compatible for this profile")
    schema = inventories[0].rank_schema

    pairs: list[SelectedTarget] = []
    for inventory in inventories:
        for candidate in inventory.candidates:
            if len(candidate.rank) != len(schema):
                raise TargetSelectionError("backend_protocol", "target rank does not match inventory schema")
            # A discovery result is trusted only for its request-bound operation.
            if not candidate.ready or operation not in candidate.capabilities:
                continue
            pairs.append(
                SelectedTarget(
                    host=inventory.host,
                    candidate=candidate,
                    backend_revision=inventory.backend_revision,
                    rank_schema=inventory.rank_schema,
                    profile_revision=inventory.profile_revision,
                )
            )
    return _best_ranked(pairs, preference=preference, preferred_role=preferred_role)
