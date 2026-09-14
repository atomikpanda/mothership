"""Client side of an exact-copy remote run: turn a working tree into a commit,
and hand that commit to the run host.

Real history goes to origin; throwaway state goes host to host and never touches
origin. This module owns the second half of that rule. `core/remote_preflight.py`
decides which repos take which path; this one carries them.
"""

from __future__ import annotations

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None

import hashlib
import json
import os
import shlex
import tempfile
from urllib.parse import quote
from pathlib import Path

from mship.core.run_host import HostRegistration, RunHostError
from mship.core.run_ref import RunRefNameError, run_ref

# Pinned identity for synthesized commits. Deliberately NOT the operator's: this
# is machinery, not a commit they made (spec ac13), and pinning it also means
# synthesis works in a repo with no `user.email` configured.
_IDENTITY_NAME = "mship run"
_IDENTITY_EMAIL = "mship-run@localhost"

_MESSAGE = "mship --remote: working-tree snapshot (throwaway, not real history)"


class RunTransferError(Exception):
    """A git command needed to synthesize or deliver the working tree failed.

    Always names the command and git's own stderr: using git's transport rather
    than a hand-rolled one is partly FOR those diagnostics, so they are passed
    through rather than replaced.
    """


def _checked(shell, command: str, cwd: Path, env: dict[str, str]) -> str:
    result = shell.run(command, cwd=cwd, env=env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RunTransferError(
            f"`{command}` failed in {cwd}: {detail or f'exit {result.returncode}'}"
        )
    return result.stdout or ""


def synthesize_commit(shell, repo_root: Path, *, base_sha: str) -> str:
    """A real commit object whose tree is byte-identical to the working tree at
    `repo_root`, created without touching the repository's own state.

    The TEMPORARY INDEX is load-bearing, not an implementation detail. `git add
    -A` against the DEFAULT index would stage the operator's work in progress —
    a destructive surprise on their real repository — so every command here runs
    with `GIT_INDEX_FILE` pointed at a scratch file that is deleted afterwards.
    HEAD, the current branch, the real index and `git status` output are all
    unchanged when this returns (verified against real git), and the commit
    belongs to no branch.

    `git read-tree <base_sha>` seeds the scratch index BEFORE `git add -A`.
    Without that seed a file that is both tracked and gitignored is silently
    dropped from the tree — git will not add an ignored path that is not already
    in the index — which is exactly the "the remote ran something subtly
    different" failure this feature exists to remove. Verified against real git.

    `base_sha` is the sha `remote_preflight.inspect` certified HEAD to be at, not
    the string `HEAD`. Re-resolving HEAD here would let anything that commits in
    this worktree between inspection and synthesis (a subagent, a background job)
    re-root the snapshot on a commit nothing verified — the same bypass
    `remote_preflight.push` closes for the origin path.

    `git commit-tree` rather than `git commit`: it writes an object and moves no
    ref, and (verified) it does not honour `commit.gpgsign`, so an operator with
    commit signing configured cannot be blocked on a passphrase prompt a
    captured-output subprocess would never show them.

    Untracked files are included — they are part of what the operator sees, and
    they now travel only between the operator's own two machines. Gitignored
    files are not; `git add -A` never picks them up.

    Safe to call from a SUBDIRECTORY of the repository (a `git_root` child):
    `git add -A` with no pathspec is whole-tree since git 2.0 and `git
    write-tree` writes the full index, so the result is the same tree either
    way. Verified.
    """
    with tempfile.TemporaryDirectory(prefix="mship-run-index-") as tmp:
        env = {
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
            "GIT_AUTHOR_NAME": _IDENTITY_NAME,
            "GIT_AUTHOR_EMAIL": _IDENTITY_EMAIL,
            "GIT_COMMITTER_NAME": _IDENTITY_NAME,
            "GIT_COMMITTER_EMAIL": _IDENTITY_EMAIL,
        }
        base = shlex.quote(base_sha)
        _checked(shell, f"git read-tree {base}", repo_root, env)
        _checked(shell, "git add -A", repo_root, env)
        tree = _checked(shell, "git write-tree", repo_root, env).strip()
        sha = _checked(
            shell,
            f"git commit-tree {shlex.quote(tree)} -p {base} -m {shlex.quote(_MESSAGE)}",
            repo_root,
            env,
        ).strip()
    return sha


def extra_header_env(token: str, url: str) -> dict[str, str]:
    """Env that makes git send `Authorization: Bearer <token>` on BOTH legs of a
    push to `url` (the `info/refs` GET and the `git-receive-pack` POST), and
    nowhere else.

    Carried as git's ENV-based config (`GIT_CONFIG_COUNT` / `_KEY_n` /
    `_VALUE_n`, git >= 2.31) rather than `git -c http.extraHeader=…`: argv is
    world-readable through `/proc/<pid>/cmdline`, a process's environment is
    not. Nothing is written to any git config file, and the token never appears
    in the remote URL (spec ac4).

    APPENDS at the next free index instead of claiming index 0 — the caller's
    environment may already carry GIT_CONFIG entries (the test suite disables
    commit signing that way, tests/conftest.py), and overwriting them would
    silently drop them. The index comes from `os.environ` because `ShellRunner.
    run` layers this dict OVER `os.environ`; the two have to be read from the
    same place or the count will not match the keys git actually receives.

    Two settings, both load-bearing:

    - `http.<url>.extraHeader` is SCOPED to the run host, so the bearer cannot
      ride a request this git process makes to anything else.
    - `http.followRedirects=false` because that scoping is not enough on its
      own. git binds the header to the request before it is sent and does not
      re-match config per hop, so a redirect to another path on the SAME origin
      carries the bearer verbatim (verified against real git; curl only strips a
      custom Authorization header when the redirect crosses origins). git's
      default, `initial`, follows exactly the redirect that matters here — the
      one on `info/refs`. Refusing it fails the push loudly instead.

    `GIT_TERMINAL_PROMPT=0` makes a rejected push FAIL rather than block on an
    interactive credential prompt that a captured-output subprocess would never
    show anyone.
    """
    settings = {
        f"http.{url}.extraHeader": f"Authorization: Bearer {token}",
        "http.followRedirects": "false",
    }
    try:
        index = max(int(os.environ.get("GIT_CONFIG_COUNT", "0")), 0)
    except ValueError:
        index = 0
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": str(index + len(settings))}
    for offset, (key, value) in enumerate(settings.items()):
        env[f"GIT_CONFIG_KEY_{index + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{index + offset}"] = value
    return env


def _receive_url(conn, repo: str, workspace_id: str | None = None) -> str:
    """The authenticated Git endpoint, workspace-bound for relay identities."""
    prefix = conn.url.rstrip("/")
    if workspace_id:
        prefix = f"{prefix}/workspaces/{quote(workspace_id, safe='')}"
    return f"{prefix}/git/{repo}"


def _push(
    shell,
    repo_root: Path,
    *,
    conn,
    refspec: str,
    url: str,
    failure: str,
    expected_sha: str | None = None,
    lease_ref: str | None = None,
) -> None:
    if expected_sha is None:
        force_option = "--force"
    else:
        if (
            not isinstance(lease_ref, str)
            or not isinstance(expected_sha, str)
            or len(expected_sha) not in (40, 64)
            or any(
                character not in "0123456789abcdefABCDEF" for character in expected_sha
            )
        ):
            raise RunTransferError("could not safely delete a recorded run ref")
        force_option = f"--force-with-lease={lease_ref}:{expected_sha}"
    result = shell.run(
        f"git push {shlex.quote(force_option)} {shlex.quote(url)} {refspec}",
        cwd=repo_root,
        env=extra_header_env(conn.token, url),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RunTransferError(f"{failure}: {detail or f'exit {result.returncode}'}")


def push_run_ref(
    shell, repo_root: Path, *, conn, workspace_id: str | None = None, repo: str, task: str, sha: str
) -> str:
    """Push `sha` straight to the run host's scratch ref, and return that ref.

    Origin is not in this path: uncommitted work — including untracked scratch
    files — goes only between the operator's own two machines.

    `git push` performs the have/want negotiation itself, so only objects the
    run host is missing cross the wire, with no need to compute what it already
    has. `--force` is required because each run replaces the last, and is safe
    precisely because nothing else writes this namespace.

    `repo` is the TOP-LEVEL git repo's name — the receive endpoint refuses a
    `git_root` child, which has no git directory of its own.
    """
    ref = run_ref(task, repo)
    url = _receive_url(conn, repo, workspace_id)
    _push(
        shell,
        repo_root,
        conn=conn,
        url=url,
        refspec=f"{shlex.quote(sha)}:{ref}",
        failure=f"could not send {repo}'s working tree to the selected run host",
    )
    return ref


def delete_run_ref(
    shell,
    repo_root: Path,
    *,
    conn,
    workspace_id: str | None = None,
    repo: str,
    task: str,
    expected_sha: str | None = None,
) -> None:
    """Delete this task's scratch ref from the run host.

    ``expected_sha`` turns deletion into a force-with-lease operation.  It is
    required for a recorded receipt so cleanup can never erase a newer transfer
    that reused the same scratch ref after the receipt was written.
    """
    ref = run_ref(task, repo)
    url = _receive_url(conn, repo, workspace_id)
    _push(
        shell,
        repo_root,
        conn=conn,
        url=url,
        refspec=f":{ref}",
        failure=f"could not delete {ref} from the selected run host",
        expected_sha=expected_sha,
        lease_ref=ref,
    )


def cleanup_run_refs(task, *, config, store, shell, warn) -> list[str]:
    """Delete unrecorded refs only after recorded exact identities are handled."""
    from mship.core.run_host import RunHostError, RunHostResolver, resolve_run_host

    resolver = RunHostResolver()
    try:
        recorded_deleted, recorded_repos = _cleanup_recorded_run_refs(
            task, config=config, store=store, shell=shell, warn=warn, resolver=resolver
        )
    except RunTransferError:
        warn("could not read recorded run refs; skipped legacy run-ref cleanup")
        return []
    deleted = list(recorded_deleted)
    seen: set[str] = set()
    repos = getattr(config, "repos", {})
    for repo in sorted(getattr(task, "affected_repos", None) or []):
        repo_config = repos.get(repo)
        if repo_config is None:
            continue
        git_repo = repo_config.git_root or repo
        if git_repo in seen or git_repo in recorded_repos:
            continue
        seen.add(git_repo)
        root_config = repos.get(git_repo)
        if root_config is None:
            continue
        try:
            host = resolve_run_host(None, repo=root_config, config=config, store=store)
            conn = resolver.resolve(host)
            delete_run_ref(
                shell,
                Path(root_config.path),
                conn=conn,
                workspace_id=getattr(host.connection, "workspace_id", None),
                repo=git_repo,
                task=task.slug,
            )
        except (RunHostError, RunTransferError, RunRefNameError):
            warn(f"could not delete {git_repo}'s run ref from the selected run host")
            continue
        deleted.append(git_repo)
    return deleted

_RECEIPTS_VERSION = 2
_RECEIPTS_FILE = "run-ref-receipts.json"


def _task_slug(task) -> str:
    slug = getattr(task, "slug", task)
    if not isinstance(slug, str) or not slug:
        raise RunTransferError("could not record a run ref without a task slug")
    return slug


def _endpoint_fingerprint(connection) -> str:
    """Stable safe registration identity; relay mode deliberately excludes route."""
    from mship.core.run_host.config import RunHostConnection, registration_identity

    if isinstance(connection, RunHostConnection):
        payload = f"mship-run-host-endpoint-v1\0{connection.url.rstrip('/')}"
    else:
        payload = "mship-run-host-identity-v2\0" + "\0".join(
            registration_identity(connection)
        )
    return hashlib.sha256(payload.encode()).hexdigest()

def _receipt_path(state_dir: Path) -> Path:
    return Path(state_dir) / _RECEIPTS_FILE


def _receipt_lock(path: Path) -> int:
    if fcntl is None:
        raise RunTransferError("recorded run-ref cleanup requires POSIX file locking")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(path.with_name(path.name + ".lock"), 0o600)
    return fd


def _load_receipts(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunTransferError("could not read recorded run refs") from exc
    if (
        not isinstance(document, dict)
        or document.get("version") not in {1, _RECEIPTS_VERSION}
        or not isinstance(document.get("receipts"), list)
    ):
        raise RunTransferError("recorded run refs have an unsupported format")
    receipts: list[dict[str, str]] = []
    for receipt in document["receipts"]:
        if (
            not isinstance(receipt, dict)
            or set(receipt)
            != {
                "task",
                "host_name",
                "host_scope",
                "host_endpoint_fingerprint",
                "repo",
                "ref",
                "sha",
            }
            or not all(isinstance(value, str) for value in receipt.values())
        ):
            raise RunTransferError("recorded run refs have an unsupported format")
        receipts.append(dict(receipt))
    return receipts


def _write_receipts(path: Path, receipts: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(
                json.dumps(
                    {"version": _RECEIPTS_VERSION, "receipts": receipts},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def record_run_ref_receipt(
    state_dir: Path, *, task, host: "HostRegistration", repo: str, ref: str, sha: str
) -> None:
    """Durably record one successful scratch-ref delivery to an exact host.

    The receipt is intentionally private and contains only safe registration
    identity plus Git source identity; bearer credentials never leave the host
    registry. Repeated delivery of the same ref to the same identity replaces
    its SHA, while a changed endpoint is retained as a separate unresolved
    receipt rather than silently retargeted.
    """

    if not isinstance(host, HostRegistration):
        raise RunTransferError("could not record a run ref for an invalid host")
    task_slug = _task_slug(task)
    try:
        expected_ref = run_ref(task_slug, repo)
    except RunRefNameError as exc:
        raise RunTransferError(str(exc)) from None
    if (
        ref != expected_ref
        or not isinstance(sha, str)
        or len(sha) not in (40, 64)
        or any(character not in "0123456789abcdefABCDEF" for character in sha)
    ):
        raise RunTransferError("could not record an invalid run-ref receipt")

    receipt = {
        "task": task_slug,
        "host_name": host.name,
        "host_scope": host.scope,
        "host_endpoint_fingerprint": _endpoint_fingerprint(host.connection),
        "repo": repo,
        "ref": ref,
        "sha": sha,
    }
    path = _receipt_path(state_dir)
    lock_fd = _receipt_lock(path)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        receipts = _load_receipts(path)
        identity = (
            receipt["task"],
            receipt["host_name"],
            receipt["host_scope"],
            receipt["host_endpoint_fingerprint"],
            receipt["repo"],
            receipt["ref"],
        )
        receipts = [
            item
            for item in receipts
            if (
                item["task"],
                item["host_name"],
                item["host_scope"],
                item["host_endpoint_fingerprint"],
                item["repo"],
                item["ref"],
            )
            != identity
        ]
        receipts.append(receipt)
        _write_receipts(path, receipts)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _delete_recorded_ref(
    shell, repo_root: Path, *, conn, workspace_id: str | None, repo: str, ref: str, expected_sha: str
) -> None:
    """Delete exactly an already-recorded ref with its recorded object lease."""
    url = _receive_url(conn, repo, workspace_id)
    _push(
        shell,
        repo_root,
        conn=conn,
        url=url,
        refspec=f":{ref}",
        failure=f"could not delete {ref} from the selected run host",
        expected_sha=expected_sha,
        lease_ref=ref,
    )


def _cleanup_recorded_run_refs(
    task, *, config, store, shell, warn, resolver
) -> tuple[list[str], set[str]]:
    """Clean only receipts whose current stable registration identity still matches."""
    task_slug = _task_slug(task)
    path = _receipt_path(store._project_path.parent)
    lock_fd = _receipt_lock(path)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        receipts = _load_receipts(path)
        recorded_repos = {receipt["repo"] for receipt in receipts if receipt["task"] == task_slug}
        try:
            current_hosts = store.effective_hosts()
        except RunHostError:
            for receipt in receipts:
                if receipt["task"] == task_slug:
                    warn(f"could not delete {receipt['repo']}'s recorded run ref: could not validate host identity")
            return [], recorded_repos
        repos = getattr(config, "repos", {})
        deleted: list[str] = []
        retained: list[dict[str, str]] = []
        for receipt in receipts:
            if receipt["task"] != task_slug:
                retained.append(receipt)
                continue
            host = current_hosts.get(receipt["host_name"])
            if host is None or host.scope != receipt["host_scope"] or _endpoint_fingerprint(host.connection) != receipt["host_endpoint_fingerprint"]:
                warn(f"could not delete {receipt['repo']}'s recorded run ref on {receipt['host_name']}: host identity changed or is unavailable")
                retained.append(receipt)
                continue
            repo_config = repos.get(receipt["repo"])
            try:
                expected_ref = run_ref(task_slug, receipt["repo"])
            except RunRefNameError:
                expected_ref = None
            if repo_config is None or receipt["ref"] != expected_ref:
                warn(f"could not delete {receipt['repo']}'s recorded run ref: recorded destination is invalid")
                retained.append(receipt)
                continue
            try:
                conn = resolver.resolve(host)
                _delete_recorded_ref(
                    shell,
                    Path(repo_config.path),
                    conn=conn,
                    workspace_id=getattr(host.connection, "workspace_id", None),
                    repo=receipt["repo"],
                    ref=receipt["ref"],
                    expected_sha=receipt["sha"],
                )
            except (RunHostError, RunTransferError, RunRefNameError):
                # A failed delete is ambiguous; never query/replay and retain the lease.
                warn(f"could not delete {receipt['repo']}'s recorded run ref; the exact ref remains recorded")
                retained.append(receipt)
                continue
            deleted.append(receipt["repo"])
        if retained != receipts:
            try:
                _write_receipts(path, retained)
            except OSError:
                warn("could not update recorded run refs after cleanup; exact receipt cleanup will retry")
        return deleted, recorded_repos
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def cleanup_recorded_run_refs(task, *, config, store, shell, warn) -> list[str]:
    """Best-effort exact-receipt cleanup, retaining any uncertain deletion."""
    from mship.core.run_host import RunHostResolver

    deleted, _recorded_repos = _cleanup_recorded_run_refs(
        task, config=config, store=store, shell=shell, warn=warn, resolver=RunHostResolver()
    )
    return deleted
