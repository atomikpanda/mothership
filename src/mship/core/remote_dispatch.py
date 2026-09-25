"""Client-side exact-source preparation shared by remote command adapters."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from mship.core.run_host import HostRegistration, RunHostError, RunHostResolver


class RemoteDispatchError(RuntimeError):
    """An actionable source-preparation failure safe to render to a user."""


@dataclass(frozen=True)
class PreparedSource:
    """The exact source identities certified for a remote dispatch.

    ``run_ref_repos`` names top-level Git repositories that the host must
    materialize from the private scratch namespace.  ``source_revisions`` is
    keyed by the originally selected configured repository, including a
    ``git_root`` child when that child shares a parent transfer.
    """

    run_ref_repos: tuple[str, ...]
    source_revisions: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_ref_repos", tuple(self.run_ref_repos))
        object.__setattr__(
            self, "source_revisions", MappingProxyType(dict(self.source_revisions))
        )


@dataclass(frozen=True)
class _RunRefSource:
    """An immutable source object waiting for delivery to a specific host."""

    git_repo: str
    path: Path
    branch: str
    sha: str


@dataclass(frozen=True)
class SourceSnapshot:
    """Certified source identities reusable across one or more run hosts.

    The snapshot deliberately contains Git object identities, not caller paths
    or a host connection.  It can therefore be delivered to several eligible
    hosts even if the local working tree changes after certification.
    """

    source_revisions: Mapping[str, str]
    _preflight: object = field(repr=False)
    _dirty_sources: tuple[_RunRefSource, ...] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_revisions", MappingProxyType(dict(self.source_revisions))
        )
        object.__setattr__(self, "_dirty_sources", tuple(self._dirty_sources))


def snapshot_remote_source(*, task_obj, target_repos, config, shell) -> SourceSnapshot:
    """Freeze certified source once, without sending it to a run host.

    Dirty trees become immutable, private commit objects now.  Clean sources
    retain the exact HEAD inspected here; their existing origin-push path is
    performed later by :func:`prepare_remote_source` with that pinned SHA.
    """
    from mship.core import remote_preflight, run_transfer

    selected = tuple(target_repos)
    pre = remote_preflight.inspect(task_obj, shell, repos=list(selected), config=config)
    if not pre.ok:
        raise RemoteDispatchError(remote_preflight.blocked_message(pre))

    revisions: dict[str, str] = {
        state.repo: state.head_sha for state in pre.states if state.head_sha is not None
    }
    dirty_sources: list[_RunRefSource] = []
    for state in pre.dirty:
        if state.head_sha is None or state.git_repo is None:
            raise RemoteDispatchError(
                "could not certify source identity for remote dispatch"
            )
        try:
            sha = run_transfer.synthesize_commit(
                shell, state.path, base_sha=state.head_sha
            )
        except run_transfer.RunTransferError:
            raise RemoteDispatchError(
                f"could not snapshot {state.git_repo}; "
                "inspect the task worktree and Git state before retrying"
            ) from None
        dirty_sources.append(
            _RunRefSource(
                git_repo=state.git_repo,
                path=state.path,
                branch=state.branch,
                sha=sha,
            )
        )
        # A git_root child shares the transferred tree. Pin every selected alias
        # to its actual snapshot rather than leaving it at the parent HEAD.
        for selected_state in pre.states:
            if selected_state.git_repo == state.git_repo:
                revisions[selected_state.repo] = sha

    missing = [repo for repo in selected if repo not in revisions]
    if missing:
        # A successful preflight cannot normally reach this case; fail closed if
        # a malformed/custom implementation did, instead of sending an unpinned
        # tool request.
        raise RemoteDispatchError(
            "could not certify source identity for remote dispatch"
        )
    return SourceSnapshot(revisions, pre, tuple(dirty_sources))


def prepare_remote_source(
    *,
    task_obj,
    target_repos,
    config,
    shell,
    host: HostRegistration,
    resolver: RunHostResolver,
    output,
    on_prepared: Callable[[str], None] | None = None,
    snapshot: SourceSnapshot | None = None,
    on_transfer: Callable[[str, str, str], None] | None = None,
    run_ref_only: bool = False,
) -> PreparedSource:
    """Transfer certified source to the selected host with a fresh Git bearer.

    When supplied, ``snapshot`` is the complete source authority: preparation
    does not inspect or synthesize the local tree again.  This prevents a later
    host from receiving a different revision merely because local files changed
    between two eligible-host transfers.
    ``run_ref_only`` sends clean and dirty objects directly to that host without
    pushing a branch or preparing its active worktree.
    """
    from mship.core import remote_preflight, run_transfer
    from mship.core.run_ref import RunRefNameError

    if snapshot is None:
        snapshot = snapshot_remote_source(
            task_obj=task_obj,
            target_repos=target_repos,
            config=config,
            shell=shell,
        )

    selected = tuple(target_repos)
    if any(repo not in snapshot.source_revisions for repo in selected):
        raise RemoteDispatchError(
            "could not certify source identity for remote dispatch"
        )

    sources = (
        {source.git_repo: source for source in snapshot._dirty_sources}
        if run_ref_only else None
    )
    if sources is not None:
        for state in snapshot._preflight.states:
            if state.repo not in selected:
                continue
            if state.git_repo is None:
                raise RemoteDispatchError("source repository identity is unavailable")
            if state.git_repo not in sources:
                sources[state.git_repo] = _RunRefSource(
                    state.git_repo, state.path, state.branch,
                    snapshot.source_revisions[state.repo],
                )
    run_ref_repos: list[str] = []
    prepared_snapshots: list[tuple[str, str, str]] | None = (
        [] if on_prepared is not None else None
    )
    for source in sources.values() if sources is not None else snapshot._dirty_sources:
        try:
            connection = resolver.resolve(host)
            ref = run_transfer.push_run_ref(
                shell,
                source.path,
                conn=connection,
                workspace_id=getattr(host.connection, "workspace_id", None),
                repo=source.git_repo,
                task=task_obj.slug,
                sha=source.sha,
            )
        except run_transfer.RunTransferError:
            raise RemoteDispatchError(
                f"could not transfer {source.git_repo} to the selected run host; "
                "verify host connectivity, pairing and repository access"
            ) from None
        except (RunRefNameError, RunHostError) as exc:
            raise RemoteDispatchError(str(exc)) from None
        # A receipt is never made for a failed push. If durable receipt creation
        # fails after a successful push, roll back only this exact ref with the
        # transferred object as a lease; a later transfer must survive.
        if on_transfer is not None:
            try:
                on_transfer(source.git_repo, ref, source.sha)
            except Exception:
                try:
                    rollback_connection = resolver.resolve(host)
                    run_transfer.delete_run_ref(
                        shell,
                        source.path,
                        conn=rollback_connection,
                        workspace_id=getattr(host.connection, "workspace_id", None),
                        repo=source.git_repo,
                        task=task_obj.slug,
                        expected_sha=source.sha,
                    )
                except (run_transfer.RunTransferError, RunRefNameError, RunHostError):
                    raise RemoteDispatchError(
                        "could not record the transferred source; exact run-ref cleanup "
                        "is unresolved"
                    ) from None
                raise RemoteDispatchError(
                    "could not record the transferred source; exact run ref was rolled back"
                ) from None
        run_ref_repos.append(source.git_repo)
        if prepared_snapshots is not None:
            prepared_snapshots.append((source.git_repo, source.sha, ref))
        output.breadcrumb(
            f"{source.git_repo}: sent your working tree to the run host as "
            f"{ref} ({source.sha[:12]}) — a throwaway run ref, not a commit on "
            f"{source.branch}"
        )

    if not run_ref_only:
        pushed, push_error = remote_preflight.push(snapshot._preflight, shell)
        if push_error is not None:
            failed_repo = snapshot._preflight.to_push[len(pushed)].repo
            raise RemoteDispatchError(
                f"could not push {failed_repo} to origin; "
                "verify Git authentication and repository access"
            )
        pushed_sha = {state.repo: state.head_sha for state in snapshot._preflight.to_push}
        for repo_name in pushed:
            sha = pushed_sha.get(repo_name)
            suffix = f" ({sha[:12]})" if sha else ""
            output.breadcrumb(
                f"pushed {repo_name}{suffix} so the run host sees your commits"
            )

    if on_prepared is not None:
        if prepared_snapshots:
            snapshots = ", ".join(
                f"{repo}@{sha[:12]} via {ref} (a throwaway run ref)"
                for repo, sha, ref in prepared_snapshots
            )
            on_prepared(
                f"an exact working-tree snapshot ({snapshots}) was sent to the run host"
            )
        else:
            commits = ", ".join(
                f"{repo}@{sha[:12]}" for repo, sha in snapshot.source_revisions.items()
            )
            on_prepared(f"task source {commits} was verified before remote dispatch")

    return PreparedSource(tuple(run_ref_repos), snapshot.source_revisions)


def run_remote_tool(
    *,
    request,
    task_obj,
    config,
    shell,
    host: HostRegistration,
    resolver: RunHostResolver,
    output,
    event_sink=None,
    transport=None,
):
    """Prepare a typed request and execute it with independently resolved credentials."""
    from mship.core.remote_client import exec_tool
    from mship.core.remote_tool import ToolRequest, ToolResult

    if (
        not isinstance(request, ToolRequest)
        or task_obj is None
        or request.task != task_obj.slug
        or request.repo not in task_obj.worktrees
    ):
        return ToolResult(status="invalid")
    if request.preparation == "observe":
        return exec_tool(
            request=request,
            host=host,
            resolver=resolver,
            event_sink=event_sink,
            transport=transport,
        )
    try:
        prepared = prepare_remote_source(
            task_obj=task_obj,
            target_repos=[request.repo],
            config=config,
            shell=shell,
            host=host,
            resolver=resolver,
            output=output,
        )
    except RemoteDispatchError as exc:
        output.error(str(exc))
        return ToolResult(status="materialization_error")
    return exec_tool(
        request=replace(
            request,
            run_ref_repos=prepared.run_ref_repos,
            source_revision=prepared.source_revisions[request.repo],
        ),
        host=host,
        resolver=resolver,
        event_sink=event_sink,
        transport=transport,
    )
