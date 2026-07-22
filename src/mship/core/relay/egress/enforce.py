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
        # push_branch must resolve to a BRANCH ref only. A bare name is a branch;
        # an explicit refs/heads/... is a branch; any other fully-qualified ref
        # (refs/tags/…, refs/notes/…) is refused — the run may push its branch, never
        # a tag or other ref (a token minted with refs/tags/v1 would otherwise
        # authorize a tag update).
        want = scope.push_branch
        if want.startswith("refs/heads/"):
            pass
        elif want.startswith("refs/"):
            raise EnforcementError(
                f"run push_branch {want!r} is not a branch; only refs/heads/ is allowed"
            )
        else:
            want = f"refs/heads/{want}"

        commands = parse_receive_pack_commands(request.body)
        if not commands:
            raise EnforcementError("receive-pack POST had no parseable ref updates")
        for cmd in commands:
            if cmd.ref != want:
                raise EnforcementError(f"push to {cmd.ref!r}; only {want!r} is allowed")
            if cmd.new_oid == _ZERO_OID:
                raise EnforcementError(f"deletion of {cmd.ref!r} is not allowed")


def classify_api_request(method: str, path: str, in_scope: bool) -> bool:
    """Pure DEFAULT-DENY classifier for the GitHub REST surface.

    Returns True to PERMIT, False to DENY. `in_scope` says whether the path's
    owner/repo is within the run's scope.repos (only meaningful for repo-scoped
    paths; the safe globals are allowed regardless). Permits ONLY:
      - GET /rate_limit, GET /user                      (safe global reads)
      - GET  /repos/{o}/{r}/...                          (reads on the run's repos)
      - POST /repos/{o}/{r}/pulls                        (OPEN a PR)
    Everything else -> deny.

    The ONLY write permitted is OPENING a PR (POST /pulls). Managing an EXISTING
    numbered PR/issue — PATCH /pulls/{n}, POST /issues/{n}/comments, PR reviews —
    is deliberately NOT permitted: those take a PR/issue number the enforcer can't
    tie to the run's own PR, so allowing them would let a (prompt-injectable) worker
    close/rewrite/review an UNRELATED PR in an in-scope repo (the run does not own
    every PR in its repos). The worker sets its title + body in the POST /pulls
    body, so it needs nothing further. Managing the run's own PR could return later
    behind per-run PR-ownership tracking (recording the PR number from the POST
    /pulls response and gating /pulls/{n} on it). Merge (PUT /pulls/{n}/merge) is a
    DIFFERENT path and is denied by construction."""
    method = method.upper()
    segs = [s for s in path.split("/") if s]

    # Safe global reads (repo-less), permitted regardless of scope.
    if method == "GET" and segs in (["rate_limit"], ["user"]):
        return True

    # Every other permit is repo-scoped: /repos/{owner}/{repo}/...
    if len(segs) < 3 or segs[0] != "repos":
        return False
    if not in_scope:
        return False
    rest = segs[3:]  # path tail after /repos/{owner}/{repo}

    # Reads on the run's repos (bounded by the repo-scoped token).
    if method == "GET":
        return True

    # The ONE permitted write: open a NEW PR. No operation on an existing numbered
    # PR/issue (the enforcer can't prove the number is the run's own PR).
    if method == "POST" and rest == ["pulls"]:
        return True
    return False


class GitHubApiEnforcer:
    """DEFAULT-DENY enforcer for the worker's api.github.com PR-egress leg.

    Permits only OPENING a PR (POST /pulls) + reads scoped to the run's repos
    (plus the safe globals /rate_limit, /user). Every repo-scoped permit
    additionally requires the path's owner/repo to be within grant.scope.repos
    (same containment as the git leg). Everything else raises EnforcementError,
    which the proxy maps to 403 — including any write to an EXISTING numbered
    PR/issue (which could target an unrelated PR), merge, POST /merges, git/refs
    mutation, and contents mutation. The API leg cannot sidestep the git
    push-to-run-branch enforcement, and cannot touch a PR the run does not own."""

    def check(self, request: EgressRequest, grant: Grant) -> None:
        in_scope = request.repo is not None and request.repo in grant.scope.repos
        if not classify_api_request(request.method, request.upstream_path, in_scope):
            raise EnforcementError(
                f"github api request refused: {request.method} {request.upstream_path} "
                f"(repo={request.repo!r}, in_scope={in_scope})"
            )


# SUPERSEDED/UNUSED placeholder: predates GitHubApiEnforcer and wired to NO route.
# Kept as flagged pre-existing dead code (do not wire to a route — a no-ref-policy
# pass-through on api.github.com would re-open the REST bypass GitHubApiEnforcer closes).
class HostLockedEnforcer:
    """No ref-level policy: the API surface is bounded by the repo-scoped App
    token + the Attachment host-lock. Passes; documents the boundary."""

    def check(self, request: EgressRequest, grant: Grant) -> None:
        return
