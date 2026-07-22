from __future__ import annotations

from typing import Protocol, runtime_checkable

from mship.core.relay.grants import Grant
from mship.core.relay.egress.pktline import parse_receive_pack_commands
from mship.core.relay.egress.request import EgressRequest

_ZERO_OID = "0" * 40


class EnforcementError(Exception):
    """A request violated the route's policy and must not reach the provider."""


@runtime_checkable
class Enforcer(Protocol):
    def check(self, request: EgressRequest, grant: Grant) -> None: ...


class GitSmartHttpEnforcer:
    """Permit clone/fetch (upload-pack) and the receive-pack advertisement;
    permit a receive-pack POST only when every ref-update targets the run's
    branch for a repo inside the run's scope. Everything else is refused."""

    def check(self, request: EgressRequest, grant: Grant) -> None:
        if request.service == "git-upload-pack":
            return
        if request.service == "git-receive-pack" and not request.is_receive_pack_post:
            return                                   # ref advertisement (read-only)
        if not request.is_receive_pack_post:
            raise EnforcementError(f"unsupported git request: {request.upstream_path}")

        scope = grant.scope
        if request.repo not in scope.repos:
            raise EnforcementError(
                f"push to {request.repo!r} outside run repos {list(scope.repos)}"
            )
        if not scope.push_branch:
            raise EnforcementError("run scope carries no push_branch; refusing all pushes")
        want = scope.push_branch
        if not want.startswith("refs/"):
            want = f"refs/heads/{want}"

        commands = parse_receive_pack_commands(request.body)
        if not commands:
            raise EnforcementError("receive-pack POST had no parseable ref updates")
        for cmd in commands:
            if cmd.ref != want:
                raise EnforcementError(f"push to {cmd.ref!r}; only {want!r} is allowed")
            if cmd.new_oid == _ZERO_OID:
                raise EnforcementError(f"deletion of {cmd.ref!r} is not allowed")


class HostLockedEnforcer:
    """No ref-level policy: the API surface is bounded by the repo-scoped App
    token + the Attachment host-lock. Passes; documents the boundary."""

    def check(self, request: EgressRequest, grant: Grant) -> None:
        return
