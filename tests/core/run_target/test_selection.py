
import pytest

from mship.core.run_host.config import HostRegistration, RunHostConnection
from mship.core.run_target.models import (
    HostInventory,
    HostRequirements,
    TargetCandidate,
    TargetSelectionError,
)
from mship.core.run_target.preferences import TargetPreference
from mship.core.run_target.selection import eligible_hosts, rank_targets


def _host(
    name: str,
    roles: tuple[str, ...] = ("ios",),
    tags: tuple[str, ...] = (),
    preference: int = 0,
    scope: str = "project",
) -> HostRegistration:
    return HostRegistration(
        name, roles, tags, preference,
        RunHostConnection(f"https://{name}.invalid", "secret"), scope,
    )


def _candidate(
    key: str,
    rank: tuple[int, ...],
    *,
    aliases: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = ("run",),
    ready: bool = True,
    reason: str | None = None,
) -> TargetCandidate:
    return TargetCandidate(
        target_key=key,
        label="iPhone simulator",
        tags=(),
        aliases=aliases,
        capabilities=capabilities,
        ready=ready,
        reason=reason,
        remediation=None,
        preparation=(),
        rank=rank,
        binding={"simulator": key},
    )


def _inventory(
    host: HostRegistration,
    *candidates: TargetCandidate,
    error: str | None = None,
    operation: str = "run",
    profile_revision: str = "exact-source-profile",
) -> HostInventory:
    return HostInventory(
        host=host,
        backend_revision="adapter-revision",
        rank_schema=("major", "minor"),
        operation=operation,
        profile_revision=profile_revision,
        candidates=candidates,
        error=error,
    )


def test_eligible_hosts_apply_roles_tags_and_project_role_pool_before_discovery():
    studio = _host("studio", tags=("desk",))
    air = _host("air", tags=("portable",))
    android = _host("android", roles=("android",), tags=("portable",))

    selected = eligible_hosts(
        {host.name: host for host in (studio, air, android)},
        allowed_roles=("ios", "android"),
        role_hosts={"ios": ("air",)},
        required=HostRequirements(roles=("ios",), tags=("portable",)),
        host_name=None,
        remote_role=None,
    )

    assert selected == (air,)


def test_explicit_host_and_remote_role_conflicts_fail_instead_of_widening_scope():
    studio = _host("studio", roles=("ios",))
    with pytest.raises(TargetSelectionError) as error:
        eligible_hosts(
            {"studio": studio},
            allowed_roles=("ios", "android"),
            role_hosts={},
            required=HostRequirements(roles=("ios",)),
            host_name="studio",
            remote_role="android",
        )
    assert error.value.code == "constraint_conflict"


def test_explicit_host_must_still_satisfy_project_role_policy():
    studio = _host("studio")
    with pytest.raises(TargetSelectionError) as error:
        eligible_hosts(
            {"studio": studio},
            allowed_roles=("ios",),
            role_hosts={"ios": ()},
            required=HostRequirements(roles=("ios",)),
            host_name="studio",
            remote_role=None,
        )
    assert error.value.code == "constraint_conflict"


def test_unknown_explicit_host_is_distinguished_from_constraint_conflict():
    with pytest.raises(TargetSelectionError) as error:
        eligible_hosts(
            {},
            allowed_roles=("ios",),
            role_hosts={},
            required=HostRequirements(roles=("ios",)),
            host_name="missing",
            remote_role=None,
        )
    assert error.value.code == "host_unknown"


def test_newest_runtime_beats_preferred_host():
    studio = _host("studio", preference=100, scope="user")
    air = _host("air", preference=0, scope="user")
    inventories = [
        _inventory(studio, _candidate("private-old", (18, 0))),
        _inventory(air, _candidate("private-new", (19, 0))),
    ]

    winners = rank_targets(inventories, profile_revision="exact-source-profile", operation="run")

    assert [(winner.host.name, winner.candidate.label) for winner in winners] == [("air", "iPhone simulator")]


def test_remembered_older_target_cannot_beat_newer_profile_rank():
    studio = _host("studio", preference=100)
    air = _host("air", preference=0)
    winners = rank_targets(
        [_inventory(studio, _candidate("old-private", (18, 0), aliases=("desk-phone",))),
         _inventory(air, _candidate("new-private", (19, 0), aliases=("travel-phone",)))],
        profile_revision="exact-source-profile",
        operation="run",
        preference=TargetPreference(host_name="studio", target_alias="desk-phone"),
    )

    assert [winner.host.name for winner in winners] == ["air"]


def test_same_rank_distinct_simulator_families_remain_a_real_tie():
    host = _host("studio")
    winners = rank_targets(
        [_inventory(host,
                    _candidate("private-ios", (18, 0), aliases=("ios",)),
                    _candidate("private-vision", (18, 0), aliases=("vision",)))],
        profile_revision="exact-source-profile",
        operation="run",
    )

    assert [winner.candidate.aliases for winner in winners] == [("ios",), ("vision",)]


def test_failed_host_inventory_never_establishes_a_unique_target_and_is_safe():
    good = _host("good")
    failed = _host("failed")
    with pytest.raises(TargetSelectionError) as error:
        rank_targets(
            [_inventory(good, _candidate("private-good", (19, 0))),
             _inventory(failed, error="backend_transport")],
            profile_revision="exact-source-profile",
            operation="run",
        )

    assert error.value.code == "discovery_incomplete"
    assert "failed" in error.value.details[0]
    assert "private-good" not in str(error.value)


def test_successful_inventory_with_no_candidates_is_not_discovery_failure():
    with pytest.raises(TargetSelectionError) as error:
        rank_targets([_inventory(_host("studio"))], profile_revision="exact-source-profile", operation="run")
    assert error.value.code == "target_unavailable"


def test_incompatible_inventory_rank_contract_fails_before_selection():
    host = _host("studio")
    incompatible = HostInventory(
        host=host,
        backend_revision="adapter-revision",
        rank_schema=("runtime",),
        operation="run",
        profile_revision="exact-source-profile",
        candidates=(_candidate("private", (19, 0)),),
        error=None,
    )
    with pytest.raises(TargetSelectionError) as error:
        rank_targets([incompatible], profile_revision="exact-source-profile", operation="run")
    assert error.value.code == "backend_protocol"


def test_disappeared_remembered_target_requires_explicit_reselection():
    host = _host("studio")
    with pytest.raises(TargetSelectionError) as error:
        rank_targets(
            [_inventory(host, _candidate("private-replacement", (19, 0), aliases=("replacement",)))],
            profile_revision="exact-source-profile",
            operation="run",
            preference=TargetPreference(host_name="studio", target_alias="former-phone"),
        )
    assert error.value.code == "target_preference_stale"


def test_requested_operation_capability_filters_higher_ranked_wrong_target():
    host = _host("studio")
    winners = rank_targets(
        [_inventory(
            host,
            _candidate("capture-only-private", (20, 0), capabilities=("capture",)),
            _candidate("run-private", (19, 0), capabilities=("run",)),
        )],
        profile_revision="exact-source-profile",
        operation="run",
    )
    assert [winner.candidate.label for winner in winners] == ["iPhone simulator"]
    assert [winner.candidate.rank for winner in winners] == [(19, 0)]


def test_no_requested_operation_capability_is_target_unavailable():
    with pytest.raises(TargetSelectionError) as error:
        rank_targets(
            [_inventory(_host("studio"), _candidate("capture-only-private", (20, 0), capabilities=("capture",)))],
            profile_revision="exact-source-profile",
            operation="run",
        )
    assert error.value.code == "target_unavailable"


def test_inventory_operation_must_match_trusted_requested_operation():
    with pytest.raises(TargetSelectionError) as error:
        rank_targets(
            [_inventory(_host("studio"), _candidate("capture-private", (20, 0), capabilities=("capture",)), operation="capture")],
            profile_revision="exact-source-profile",
            operation="run",
        )
    assert error.value.code == "backend_protocol"


def test_inventory_profile_revision_must_match_requested_profile_before_ranking():
    with pytest.raises(TargetSelectionError) as error:
        rank_targets(
            [_inventory(_host("studio"), _candidate("private", (19, 0)), profile_revision="different-profile")],
            profile_revision="exact-source-profile",
            operation="run",
        )
    assert error.value.code == "backend_protocol"


def test_matching_inventory_profile_revision_is_preserved_on_selected_target():
    winner = rank_targets(
        [_inventory(_host("studio"), _candidate("private", (19, 0)), profile_revision="exact-source-profile")],
        profile_revision="exact-source-profile",
        operation="run",
    )[0]
    assert winner.profile_revision == "exact-source-profile"
