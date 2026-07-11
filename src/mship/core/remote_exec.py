"""Serve-side execution of a go-task verb on THIS machine — the "remote" side
of `mship run/capture/build --remote[=role]` (see
specs/2026-07-11-remote-run-machine.md, MOS-191/MOS-203).

The client (a different mship workspace, Task 5) POSTs `{task, repos,
platform?, kind?}` to `POST /exec/{verb}` (registered in `core/serve.py`,
inheriting serve's bearer-auth dependency). This module is the generator
`run_verb_stream` that endpoint drives: it materializes the task's branch
worktree on this machine (the branch already exists on origin — pushed by
the operator's `mship spawn`/dispatch — this never creates a new branch),
runs the repo's go-task target with the same env-var contract as a local
run, and yields the subprocess's output incrementally so the client can
render it live.

Wire contract (Task 5's client parses this):
    - Every line yielded up to the last one is raw task output (stdout and
      stderr interleaved, one write per line, newline-terminated, UTF-8).
    - Any freeform notice lines (e.g. the MOS-203 base-freshness warning)
      are indistinguishable from task output on the wire — they're just
      more lines — so a client that renders every line live is correct by
      construction; nothing needs it to distinguish notice vs. task output.
    - The LAST line always matches `__MSHIP_EXIT__ <code>\\n` where <code>
      is a base-10 int (0 == success). This conveys the remote task's exit
      code as DATA, never as an HTTP error status — a non-zero task exit
      does not raise; the client must parse this trailing line to learn
      the real result and mirror it as its own process exit code.
"""
from __future__ import annotations

import queue
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from mship.core import capture as _cap
from mship.core.config import WorkspaceConfig

VERBS: tuple[str, ...] = ("run", "capture", "build")

# Trailing sentinel line: `f"{EXIT_MARKER} {code}\n"`. See module docstring
# for the full wire contract Task 5's client must parse.
EXIT_MARKER = "__MSHIP_EXIT__"


class UnknownVerbError(ValueError):
    """`verb` is not one of `VERBS`."""


class ShellLike(Protocol):
    """The subset of `mship.util.shell.ShellRunner` this module needs.
    `.run` issues git plumbing (fetch/worktree add/reset, base-freshness
    probes); `.run_streaming` launches the go-task target itself so its
    output can be drained incrementally. Same shapes as the real
    `ShellRunner` — tests inject a fake implementing just this surface."""

    def run(self, command: str, cwd: Path, env: dict[str, str] | None = None): ...

    def run_streaming(self, command: str, cwd: Path, env: dict[str, str] | None = None): ...

    def build_command(self, command: str, env_runner: str | None = None) -> str: ...


@dataclass
class RemoteExecDeps:
    """Collaborators `run_verb_stream` needs, injected so tests can fake
    them without touching a real filesystem/subprocess/git checkout.

    `config` is THIS machine's own `WorkspaceConfig` (its own repo paths —
    which generally differ from the operator's machine's paths, hence this
    can't be sent over the wire and must be resolved locally). `shell` does
    double duty for git commands and the streamed task run (see
    `ShellLike`). `workspace_root` anchors where remote worktrees live:
    `<workspace_root>/.worktrees/<task>/<repo>`, mirroring the local
    `WorktreeManager` hub layout.
    """

    config: WorkspaceConfig
    shell: ShellLike
    workspace_root: Path


def _hub_dir(workspace_root: Path, task: str) -> Path:
    return workspace_root / ".worktrees" / task


def _git_rev(shell: ShellLike, cwd: Path, ref: str) -> str | None:
    result = shell.run(f"git rev-parse {ref}", cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def check_base_freshness(
    shell: ShellLike, repo_path: Path, base_branch: str | None
) -> str | None:
    """MOS-203: before materializing, make sure this repo's knowledge of the
    task's base branch is current — auto-fetch it and, if origin had moved,
    return a warning line (str, no trailing newline) to surface into the
    stream. A remote run that silently sat on a stale base would build old
    code with no visible signal, hence the fold-in.

    Returns None when there's no base_branch to check, or when the fetch
    didn't move anything (already current) — a warning is only emitted when
    origin genuinely had new commits we didn't have yet.
    """
    if not base_branch:
        return None
    before = _git_rev(shell, repo_path, f"origin/{base_branch}")
    shell.run(f"git fetch origin {base_branch}", cwd=repo_path)
    after = _git_rev(shell, repo_path, f"origin/{base_branch}")
    if before is not None and after is not None and before != after:
        return (
            f"warning: base '{base_branch}' was behind origin "
            f"({before[:7]} -> {after[:7]}) — auto-fetched before materializing."
        )
    return None


def materialize_worktree(
    shell: ShellLike, repo_path: Path, worktree_path: Path, branch: str
) -> None:
    """Ensure `worktree_path` is a git worktree of `repo_path` sitting on
    `branch`, fresh from origin.

    `branch` already exists on origin (created by `mship spawn`/dispatch on
    the operator's machine and pushed there) — this NEVER creates a new
    branch here, it only fetches + tracks the existing one. Idempotent: a
    worktree that already exists is fetched + hard-reset to the new tip
    rather than re-added (the remote branch may have moved since the last
    remote run); a first-time run creates it with `git worktree add -B`,
    which is safe to re-run even if a stale local branch ref of the same
    name exists (e.g. left over from a removed worktree).
    """
    shell.run(f"git fetch origin {branch}", cwd=repo_path)
    if (worktree_path / ".git").exists():
        shell.run(f"git fetch origin {branch}", cwd=worktree_path)
        shell.run(f"git checkout {branch}", cwd=worktree_path)
        shell.run(f"git reset --hard origin/{branch}", cwd=worktree_path)
    else:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        shell.run(
            f"git worktree add -B {branch} {worktree_path} origin/{branch}",
            cwd=repo_path,
        )


def _drain_to_queue(proc, q: "queue.Queue[str]") -> list[threading.Thread]:
    """Start daemon threads that read `proc.stdout`/`proc.stderr` line by
    line and push each line onto `q` as it's produced — the same drain
    pattern as `util.stream_printer.drain_to_printer`, but feeding a queue
    a generator can pull from instead of a printer."""

    def _drain(stream):
        if stream is None:
            return
        try:
            while True:
                line = stream.readline()
                if not isinstance(line, str) or line == "":
                    break
                q.put(line)
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    threads = [
        threading.Thread(target=_drain, args=(proc.stdout,), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr,), daemon=True),
    ]
    for t in threads:
        t.start()
    return threads


def _stream_proc_lines(proc) -> Iterator[bytes]:
    """Yield each stdout/stderr line from `proc` as UTF-8 bytes AS IT'S
    PRODUCED (not buffered until the process exits) — this is what makes
    `run_verb_stream` a live stream rather than a final blob. Returns once
    both drain threads have hit EOF and the queue is empty."""
    q: "queue.Queue[str]" = queue.Queue()
    threads = _drain_to_queue(proc, q)
    while any(t.is_alive() for t in threads) or not q.empty():
        try:
            line = q.get(timeout=0.05)
        except queue.Empty:
            continue
        yield line.encode("utf-8", errors="replace")
    for t in threads:
        t.join(timeout=1.0)


def run_verb_stream(
    verb: str,
    task: str,
    repos: list[str],
    platform: str | None,
    *,
    deps: RemoteExecDeps,
    kind: str = "all",
) -> Iterator[bytes]:
    """The serve-side body of `POST /exec/{verb}`.

    A plain sync generator — the FastAPI endpoint runs it off the event
    loop (e.g. via `starlette.concurrency.iterate_in_threadpool`) since it
    does blocking subprocess/git I/O. For each repo, in order:

      1. MOS-203 base-freshness check (auto-fetch + an optional warning
         line — see `check_base_freshness`).
      2. Materialize `<workspace_root>/.worktrees/<task>/<repo>` on the
         task's branch (see `materialize_worktree`). `git_root` (nested
         subdirectory) repos skip their own fetch/worktree-add and resolve
         to a path under their parent's worktree instead, mirroring
         `WorktreeManager.spawn`'s treatment of subdirectory services.
      3. Resolve the repo's go-task target for `verb` + its env_runner;
         for `verb == "capture"` build the same env-var contract as local
         capture (`MSHIP_CAPTURE_DIR` a fresh remote temp dir,
         `MSHIP_CAPTURE_KINDS`, `MSHIP_CAPTURE_PLATFORM`) — see
         `mship.core.capture.run_capture`.
      4. Run it via `ShellRunner.run_streaming`, yielding each stdout/stderr
         line as it's produced.
      5. Stop at the first repo whose task exits non-zero (fail-fast,
         mirroring `RepoExecutor`'s tier behavior) rather than continuing
         to the next repo.

    Always ends with the trailing sentinel line `f"{EXIT_MARKER} {code}\\n"`
    conveying the run's exit code as DATA — never raises for a non-zero
    task exit (see module docstring for the full wire contract).
    """
    if verb not in VERBS:
        raise UnknownVerbError(
            f"unknown verb {verb!r}; expected one of {VERBS}"
        )

    kinds: list[str] | None = None
    if verb == "capture":
        kinds = _cap.resolve_kinds(kind)

    config = deps.config
    shell = deps.shell
    hub = _hub_dir(deps.workspace_root, task)
    branch = config.branch_pattern.replace("{slug}", task)

    materialized: dict[str, Path] = {}
    exit_code = 0

    for repo_name in repos:
        repo_config = config.repos[repo_name]

        if repo_config.git_root is not None:
            # Subdirectory child (mirrors WorktreeManager.spawn): its git
            # tree is the parent's, already fetched/checked-out above (or
            # by a prior remote run) — no independent fetch/worktree-add.
            parent_wt = materialized.get(repo_config.git_root, hub / repo_config.git_root)
            worktree_path = parent_wt / repo_config.path
        else:
            repo_path = repo_config.path
            worktree_path = hub / repo_name

            warning = check_base_freshness(shell, repo_path, repo_config.base_branch)
            if warning is not None:
                yield f"{warning}\n".encode("utf-8")

            materialize_worktree(shell, repo_path, worktree_path, branch)

        materialized[repo_name] = worktree_path

        actual_task_name = repo_config.tasks.get(verb, verb)
        env_runner = repo_config.env_runner or config.env_runner

        env: dict[str, str] | None = None
        if verb == "capture":
            out_dir = Path(tempfile.mkdtemp(prefix="mship-remote-capture-"))
            env = {
                "MSHIP_CAPTURE_DIR": str(out_dir),
                "MSHIP_CAPTURE_KINDS": ",".join(kinds or []),
            }
            if platform is not None:
                env["MSHIP_CAPTURE_PLATFORM"] = platform

        command = shell.build_command(f"task {actual_task_name}", env_runner)
        proc = shell.run_streaming(command, cwd=worktree_path, env=env)
        yield from _stream_proc_lines(proc)
        exit_code = proc.wait()
        if exit_code != 0:
            break

    yield f"{EXIT_MARKER} {exit_code}\n".encode("utf-8")
