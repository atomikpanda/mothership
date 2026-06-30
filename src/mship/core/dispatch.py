"""Build the agent-agnostic subagent-prompt emitted by `mship dispatch`.

Pure builder — zero I/O, trivially unit-testable. The CLI wrapper in
src/mship/cli/dispatch.py handles resolution, subprocess calls, and stdout.

See docs/superpowers/specs/2026-04-17-mship-dispatch-design.md.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mship.core.log import LogEntry
from mship.core.state import Task


_CANONICAL_SKILL_NAMES: tuple[str, ...] = (
    "working-with-mothership",
    "test-driven-development",
    "finishing-a-development-branch",
    "verification-before-completion",
)


_TASK_OPEN_RE = re.compile(r"<!--\s*mship:task\s+id=([^\s>]+)\s*-->")
_TASK_CLOSE_RE = re.compile(r"<!--\s*/mship:task\s*-->")


def extract_plan_task(plan_text: str, task_id: str) -> str:
    """Return the inner content of the anchored task block whose id matches.

    A task block is delimited by `<!-- mship:task id=<task_id> -->` and the
    next `<!-- /mship:task -->`. The returned text is the content between the
    anchors, with surrounding whitespace stripped. Pure — no I/O.

    Raises ValueError when the id is missing, appears more than once, or the
    block is unterminated (no closing anchor before the next open anchor / EOF).
    """
    opens = [m for m in _TASK_OPEN_RE.finditer(plan_text) if m.group(1) == task_id]
    if not opens:
        raise ValueError(
            f"no task with id {task_id!r} in plan "
            f"(expected an anchor `<!-- mship:task id={task_id} -->`)"
        )
    if len(opens) > 1:
        raise ValueError(
            f"duplicate task id {task_id!r} in plan ({len(opens)} anchors)"
        )
    open_m = opens[0]
    close_m = _TASK_CLOSE_RE.search(plan_text, open_m.end())
    next_open = _TASK_OPEN_RE.search(plan_text, open_m.end())
    if close_m is None or (next_open is not None and next_open.start() < close_m.start()):
        raise ValueError(
            f"unterminated task block for id {task_id!r} "
            f"(missing closing `<!-- /mship:task -->`)"
        )
    return plan_text[open_m.end():close_m.start()].strip()


@dataclass(frozen=True)
class SkillRef:
    name: str
    path: Path


def canonical_skills(pkg_skills_source: Path) -> list[SkillRef]:
    """Return the four canonical skills every dispatched subagent should read."""
    return [
        SkillRef(name=n, path=pkg_skills_source / n / "SKILL.md")
        for n in _CANONICAL_SKILL_NAMES
    ]


def resolve_repo(task: Task, repo_flag: str | None) -> str:
    """Pick which repo's worktree the dispatch prompt targets.

    Priority: --repo flag > task.active_repo > sole worktree > ValueError.
    """
    if repo_flag is not None:
        if repo_flag not in task.worktrees:
            raise ValueError(
                f"unknown repo: {repo_flag!r}. "
                f"Task affects: {sorted(task.worktrees)}"
            )
        return repo_flag
    if task.active_repo and task.active_repo in task.worktrees:
        return task.active_repo
    if len(task.worktrees) == 1:
        return next(iter(task.worktrees))
    raise ValueError(
        f"task {task.slug!r} affects {len(task.worktrees)} repos and no "
        f"active_repo is set; pass --repo <name> or run mship switch <repo> "
        f"first. Affected repos: {sorted(task.worktrees)}"
    )


@dataclass(frozen=True)
class BaseShaInfo:
    base_sha: str | None         # local <base_branch>
    origin_base_sha: str | None  # remote origin/<base_branch>
    head_sha: str                # current HEAD of the worktree
    ahead_of_base: int | None
    base_behind_origin: int | None
    has_upstream: bool
    summary: str                 # one-line human-readable


def _git_out(args: list[str], cwd: Path, timeout: int = 10) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def collect_base_sha_info(worktree: Path, base_branch: str) -> BaseShaInfo:
    """Probe local `<base>`, `origin/<base>`, and HEAD. Graceful on missing upstream."""
    head_sha = _git_out(["rev-parse", "--short", "HEAD"], cwd=worktree) or "?"
    base_sha = _git_out(["rev-parse", "--short", base_branch], cwd=worktree)  # spec: always local
    origin_base_sha = _git_out(
        ["rev-parse", "--short", f"origin/{base_branch}"], cwd=worktree,
    )
    has_upstream = origin_base_sha is not None

    ahead_of_base: int | None = None
    base_behind_origin: int | None = None
    if base_sha:
        out = _git_out(["rev-list", "--count", f"{base_branch}..HEAD"], cwd=worktree)
        try:
            ahead_of_base = int(out) if out is not None else None
        except ValueError:
            ahead_of_base = None
    if base_sha and has_upstream:
        out = _git_out(
            ["rev-list", "--count", f"{base_branch}..origin/{base_branch}"],
            cwd=worktree,
        )
        try:
            base_behind_origin = int(out) if out is not None else None
        except ValueError:
            base_behind_origin = None

    summary = _summarize_base_sha(
        ahead_of_base=ahead_of_base,
        base_behind_origin=base_behind_origin,
        has_upstream=has_upstream,
        base_branch=base_branch,
    )
    return BaseShaInfo(
        base_sha=base_sha, origin_base_sha=origin_base_sha, head_sha=head_sha,
        ahead_of_base=ahead_of_base, base_behind_origin=base_behind_origin,
        has_upstream=has_upstream, summary=summary,
    )


def _summarize_base_sha(
    *, ahead_of_base: int | None, base_behind_origin: int | None,
    has_upstream: bool, base_branch: str,
) -> str:
    parts = []
    if not has_upstream:
        parts.append(f"no upstream tracked for `{base_branch}`")
    elif base_behind_origin == 0:
        parts.append(f"base is in sync with origin")
    elif base_behind_origin and base_behind_origin > 0:
        plural = "s" if base_behind_origin != 1 else ""
        parts.append(f"base is {base_behind_origin} commit{plural} behind origin")
    if ahead_of_base is not None:
        plural = "s" if ahead_of_base != 1 else ""
        if ahead_of_base == 0:
            parts.append(f"HEAD is at base")
        else:
            parts.append(f"HEAD is {ahead_of_base} commit{plural} ahead of base")
    return "; ".join(parts) if parts else "unknown"


DISPATCH_MODES: tuple[str, ...] = ("implementer", "standalone")


_CONVENTIONS_INTRO = "These are strictly enforced in this workspace:"

_CONV_NO_MAIN_CHECKOUT = "- **Don't edit from the main checkout.** Only the worktree path above. The pre-commit hook refuses otherwise."

_CONV_BYPASS = "- **Prefer `--bypass-<check>` over `--force-<check>`** on any mship command that takes one (e.g., `--bypass-reconcile`, `--bypass-audit`). Different flag name if you see `--force-<something>` in older docs; the bypass form is canonical."

# Standalone dispatches own their own finish: the subagent opens the PR.
_CONV_OPEN_PR = "- **Use `mship finish --body-file <path>` to open the PR.** Empty bodies are rejected by design. Write a real Summary and Test plan."

# Implementer dispatches hand back to an orchestrator that integrates and finishes.
_CONV_NO_PR = "- **Don't open a PR or run `mship finish`.** The orchestrator integrates, reviews, and finishes after your hand-off — report back instead (see below)."


def _conventions_recap(mode: str) -> str:
    finish_bullet = _CONV_OPEN_PR if mode == "standalone" else _CONV_NO_PR
    return "\n".join([_CONVENTIONS_INTRO, "", finish_bullet, _CONV_NO_MAIN_CHECKOUT, _CONV_BYPASS])


# Standalone: the subagent finishes and opens the PR itself.
_FINISH_CONTRACT = """\
When the work is done:

1. Run `mship test` until green (or confirm no test suite applies).
2. Write a PR body as a file — Summary + Test plan.
3. Run `mship finish --body-file <path>` in the worktree.
4. Return the PR URL in your final message.

If you get stuck or find the task is wrong-shaped, stop and report back with what you tried and where you're blocked. Don't guess.
"""


# Implementer: scoped to a single task, hands control back without opening a PR.
# This is the default — per-task implementers in an orchestrated flow let the
# orchestrator own integration and PR creation (#234).
_REPORT_CONTRACT = """\
You are a **per-task implementer**: implement exactly the one task described above, then hand control back to the orchestrator. The orchestrator — not you — integrates, reviews, and opens the PR (`mship finish`). **Do not open a PR yourself.**

1. **Before coding**, ask any clarifying questions if the task is ambiguous or underspecified — don't guess at intent.
2. Implement only this one task. Don't pick up adjacent work or the rest of a plan.
3. Run `mship test` until green (or confirm no test suite applies).
4. Self-review your change against the instruction above.
5. **Return a status report** as your final message: what changed (files/functions), what you verified (tests/commands run), and anything left or uncertain.

If you get stuck or the task is wrong-shaped, stop and report what you tried and where you're blocked. Don't guess.
"""


def _closing_section(mode: str) -> tuple[str, str]:
    """Return the (heading, body) for the prompt's closing section."""
    if mode == "standalone":
        return "How to finish", _FINISH_CONTRACT
    return "Report back (do not open a PR)", _REPORT_CONTRACT


def _render_base_sha_block(info: BaseShaInfo, base_branch: str) -> str:
    origin_val = info.origin_base_sha if info.has_upstream else "(no upstream)"
    return (
        "```\n"
        f"base ({base_branch})  @ {info.base_sha or '?'}\n"
        f"origin/{base_branch}  @ {origin_val}\n"
        f"HEAD                 @ {info.head_sha}    ({info.summary})\n"
        "```"
    )


def _render_journal(entries: list[LogEntry]) -> str:
    if not entries:
        return "*No entries yet — this task hasn't logged anything; your instruction above is the whole picture.*"
    lines = []
    for e in entries:
        ts = e.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        meta_parts = []
        if e.iteration is not None:
            meta_parts.append(f"iter={e.iteration}")
        if e.test_state:
            meta_parts.append(f"test={e.test_state}")
        if e.action:
            meta_parts.append(f'action="{e.action}"')
        meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
        msg = e.message.splitlines()[0] if e.message else ""
        lines.append(f"- **{ts}**{meta} — {msg}")
    return "\n".join(lines)


def _render_skills(skills: list[SkillRef]) -> str:
    return "\n".join(f"- `{s.name}` — `{s.path}`" for s in skills)


def _format_dependencies_section(task: Task, state=None) -> str:
    """Format Dependencies section if task has upstream dependencies."""
    if not task.depends_on:
        return ""
    lines = ["## Dependencies", ""]
    if state is not None:
        from mship.core.task_graph import is_ready
        for edge in task.depends_on:
            ready = is_ready(state, edge.upstream_slug, {})
            mark = "ready" if ready else "not ready"
            lines.append(f"- depends on: `{edge.upstream_slug}` ({mark})")
    else:
        for edge in task.depends_on:
            lines.append(f"- depends on: `{edge.upstream_slug}`")
    lines.append("")
    return "\n".join(lines)


def build_dispatch_prompt(
    task: Task,
    repo: str,
    instruction: str,
    *,
    journal_entries: list[LogEntry],
    base_sha_info: BaseShaInfo,
    agents_md_path: Path | None,
    pkg_skills_source: Path,
    state=None,
    mode: str = "implementer",
) -> str:
    """Return the full markdown dispatch prompt for a fresh subagent.

    `mode` selects the closing framing (#234):
    - "implementer" (default): scope to a single task, report back, do NOT open
      a PR — for per-task execution where an orchestrator owns finishing.
    - "standalone": the subagent finishes the work and opens its own PR.
    """
    if mode not in DISPATCH_MODES:
        raise ValueError(
            f"unknown dispatch mode {mode!r}; choose one of {', '.join(DISPATCH_MODES)}"
        )
    worktree = task.worktrees[repo]
    base_branch = task.base_branch or "main"
    skills_block = _render_skills(canonical_skills(pkg_skills_source))
    journal_block = _render_journal(journal_entries)
    base_block = _render_base_sha_block(base_sha_info, base_branch)
    dependencies_block = _format_dependencies_section(task, state=state)
    conventions_recap = _conventions_recap(mode)
    closing_heading, closing_body = _closing_section(mode)
    agents_line = f"\nFull doc: `{agents_md_path}`." if agents_md_path else ""

    return f"""\
# Task: {task.slug}

You are a subagent dispatched to work on an in-progress mothership task.

## Work from (mandatory)

Before editing anything: `cd {worktree}`

This is a git worktree checked out on branch `{task.branch}`. Every edit, test run, and commit happens inside this directory. Do not edit from the main checkout — the mship pre-commit hook will refuse and you'll waste a cycle.

## Your instruction

> {instruction}

## Task facts

- **slug:** {task.slug}
- **branch:** {task.branch}
- **base branch:** {base_branch}
- **active repo:** {repo}

{dependencies_block}
## Where the branch stands

{base_block}

## Recent journal (last 10 entries)

{journal_block}

## Conventions (recap)

{conventions_recap}{agents_line}

## Read these skills before starting

Invoke via your platform's skill tool if it has one. Direct read paths (always valid; skills ship with mship):

{skills_block}

## {closing_heading}

{closing_body}"""
