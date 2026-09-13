"""Client-side exact-source preparation shared by remote command adapters."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Callable, Mapping

from mship.core.run_host import RunHostConnection


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


def prepare_remote_source(
    *,
    task_obj,
    target_repos,
    config,
    shell,
    conn: RunHostConnection,
    output,
    on_prepared: Callable[[str], None] | None = None,
) -> PreparedSource:
    """Certify and transfer exactly the selected task source before dispatch.

    Clean commits are pushed to origin only when the preflight has certified
    the exact HEAD; dirty trees are committed into the private run-ref namespace
    and recorded only after that transfer succeeds.  The output text intentionally
    matches the existing remote CLI path so capture provenance remains stable.
    """
    from mship.core import remote_preflight, run_transfer
    from mship.core.run_ref import RunRefNameError

    pre = remote_preflight.inspect(
        task_obj, shell, repos=list(target_repos), config=config
    )
    if not pre.ok:
        raise RemoteDispatchError(remote_preflight.blocked_message(pre))

    revisions: dict[str, str] = {
        state.repo: state.head_sha for state in pre.states if state.head_sha is not None
    }
    run_ref_repos: list[str] = []
    prepared_snapshots: list[tuple[str, str, str]] | None = (
        [] if on_prepared is not None else None
    )

    for state in pre.dirty:
        try:
            sha = run_transfer.synthesize_commit(
                shell, state.path, base_sha=state.head_sha
            )
            ref = run_transfer.push_run_ref(
                shell,
                state.path,
                conn=conn,
                repo=state.git_repo,
                task=task_obj.slug,
                sha=sha,
            )
        except (run_transfer.RunTransferError, RunRefNameError) as exc:
            raise RemoteDispatchError(str(exc)) from None
        run_ref_repos.append(state.git_repo)
        # A git_root child shares the transferred tree. Pin every selected alias
        # to its actual snapshot rather than leaving it at the parent HEAD.
        for selected in pre.states:
            if selected.git_repo == state.git_repo:
                revisions[selected.repo] = sha
        if prepared_snapshots is not None:
            prepared_snapshots.append((state.git_repo, sha, ref))
        output.breadcrumb(
            f"{state.git_repo}: sent your working tree to the run host as "
            f"{ref} ({sha[:12]}) — a throwaway run ref, not a commit on "
            f"{state.branch}"
        )

    pushed, push_error = remote_preflight.push(pre, shell)
    if push_error is not None:
        raise RemoteDispatchError(push_error)
    pushed_sha = {state.repo: state.head_sha for state in pre.to_push}
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
            states = [state for state in pre.states if state.repo in target_repos]
            commits = ", ".join(
                f"{state.repo}@{(state.head_sha or 'unknown')[:12]}" for state in states
            )
            on_prepared(f"task source {commits} was verified before remote dispatch")

    missing = [repo for repo in target_repos if repo not in revisions]
    if missing:
        # A successful preflight cannot normally reach this case; fail closed if
        # a malformed/custom implementation did, instead of sending an unpinned
        # tool request.
        raise RemoteDispatchError(
            "could not certify source identity for remote dispatch"
        )
    return PreparedSource(tuple(run_ref_repos), revisions)


def run_remote_tool(
    *,
    request,
    task_obj,
    config,
    shell,
    conn: RunHostConnection,
    output,
    event_sink=None,
    transport=None,
):
    """Prepare a typed tool request once, then execute it against ``conn``.

    Discovery and launch use the same exact-source proof as legacy remote
    commands and overwrite caller-provided source/ref claims with certified
    values.  Observation is intentionally different: it validates only the
    already-resolved task identity, never transfers or rematerializes source,
    and keeps the supplied pinned connection and opaque owner pair intact.
    """
    from mship.core.remote_client import exec_tool
    from mship.core.remote_tool import ToolRequest, ToolResult

    if (
        not isinstance(request, ToolRequest)
        or task_obj is None
        or request.task != task_obj.slug
    ):
        return ToolResult(status="invalid")
    if request.repo not in task_obj.worktrees:
        return ToolResult(status="invalid")

    if request.preparation == "observe":
        return exec_tool(
            request=request,
            conn=conn,
            event_sink=event_sink,
            transport=transport,
        )

    try:
        prepared = prepare_remote_source(
            task_obj=task_obj,
            target_repos=[request.repo],
            config=config,
            shell=shell,
            conn=conn,
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
        conn=conn,
        event_sink=event_sink,
        transport=transport,
    )
