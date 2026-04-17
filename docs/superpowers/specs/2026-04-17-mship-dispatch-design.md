# mship dispatch — Design

## Context

Mothership tasks run in their own worktrees. Today, every time a user or parent-agent wants to hand a task off to a subagent, they have to hand-assemble the context: the worktree path, the branch, recent journal entries, the skill files to read, the conventions not to violate. That assembly is tedious, error-prone (cwd-pollution bugs from stale absolute paths — see #46), and tightly couples each agent framework (Claude Code, codex, gemini-cli) to the parent's prompt-crafting code.

Issue #52 proposes a single primitive to eliminate that friction: `mship dispatch` emits a self-contained subagent prompt to stdout. Callers pipe the prompt into whatever agent framework they prefer. mship stays agent-shaped (prompt in, no launch), not agent-coupled (no hardcoded `claude -p` etc.).

This is v1 of the v2-multi-agent-orchestration roadmap item: once dispatch exists, launching N subagents in parallel is a matter of N dispatch invocations plus a coordinating harness (out of scope for v1).

## Goal

Emit a self-contained, agent-agnostic markdown prompt for a resolved mship task that a cold-start subagent can execute without a second probe call. The prompt tells the subagent where to `cd`, what's been done (last 10 journal entries), where the branch stands (base/upstream/HEAD SHAs + human summary), which skills to read first (the canonical four), which workspace conventions are enforced (3-bullet recap + AGENTS.md path), what to do (user's instruction verbatim), and how to finish (`mship finish --body-file` + return PR URL).

## Success criterion

From a single-repo task's worktree:

```bash
mship dispatch -i "implement the --title flag from #45"
```

prints a markdown document that, when passed verbatim to a fresh subagent (`claude -p "$(mship dispatch -i '...')"` or platform equivalent), lets that subagent `cd` to the right worktree, invoke the four canonical skills, read the last journal entries, and execute the instruction — without any additional probes from the parent.

## Anti-goals (v1)

- **No launcher wrapper.** Prompt-on-stdout is the protocol. `--exec <agent-cli>` forecloses the tmux/matrix/SDK-dispatch patterns and couples mship to each agent's CLI; explicitly rejected in the issue body.
- **No `--json` output.** Markdown is the load-bearing primitive. JSON is a valid v2 addition; keep v1 tight.
- **No progress-event callbacks / multi-agent coordination.** Dispatch is one-shot. Subagent writes to `mship journal`; parent polls `mship status` or reads the journal. That's enough for v1.
- **No task-type heuristics** for tailoring the skill list. Static canonical four.
- **No cross-repo single-prompt mode.** For a task affecting N repos, v1 emits N prompts — one per dispatch call, each scoped to a single worktree. v2 multi-agent orchestration can stitch them.
- **No dynamic AGENTS.md parsing.** Hardcoded 3-bullet recap + path reference. The recap rarely changes; when it does, it changes faster than mship releases anyway.

## Architecture

Mirrors the `mship context` / `mship skill` pattern already in the codebase:

- **`src/mship/core/dispatch.py`** — pure builder module. Zero I/O, trivially unit-testable.
- **`src/mship/cli/dispatch.py`** — thin Typer wrapper. Resolves task via existing `resolve_or_exit`, gathers inputs, calls the builder, prints to stdout.

### Core builder

```python
def build_dispatch_prompt(
    task: Task,
    repo: str,
    instruction: str,
    *,
    journal_entries: list[LogEntry],
    base_sha_info: BaseShaInfo,
    agents_md_path: Path | None,
    pkg_skills_source: Path,
) -> str: ...
```

Takes fully-resolved inputs, returns the markdown document. Helpers in the same module:

- `resolve_repo(task: Task, repo_flag: str | None) -> str` — picks the worktree per resolution order (below).
- `collect_base_sha_info(worktree: Path, base_branch: str) -> BaseShaInfo` — subprocess-wrapped `git rev-parse` + `git rev-list --count` calls against `HEAD`, `<base>`, `origin/<base>`; returns a dataclass with three SHAs + a human-readable `summary` string. Gracefully degrades on missing upstream / missing remote.
- `canonical_skills(pkg_skills_source: Path) -> list[SkillRef]` — returns the hardcoded four with full paths under `<pkg_skills_source>/<name>/SKILL.md`.

### Repo resolution order

For a task with `worktrees: dict[str, Path]`:

1. `--repo <name>` flag — explicit override, highest priority.
2. `task.active_repo` field (set by `mship switch <repo>`).
3. If `task.worktrees` has exactly one entry, use it.
4. Else exit 1 with message: `task "<slug>" affects N repos and no active_repo is set; pass --repo <name> or run mship switch <repo> first. Affected repos: <list>`.

### CLI contract

```
mship dispatch [--task <slug>] [--repo <name>] -i "<instruction>"
```

- Task resolution uses the existing `resolve_or_exit(state, cli_task)` helper — identical semantics to `mship journal`, `mship finish`, `mship block`, etc. Priority: `--task` flag > `MSHIP_TASK` env > cwd → worktree → task. Falls back cleanly when there's exactly one active task.
- `--instruction` / `-i` is **required**. No default ("continue this task"); keeps the command's intent unambiguous.
- Output: markdown to stdout. Exit 0 on success; exit 1 on unresolvable task or repo.

## Prompt template

Sections in this order (concrete example follows):

1. **Title** — `# Task: <slug>` + one-liner "You are a subagent dispatched to work on an in-progress mothership task."
2. **Work from (mandatory)** — worktree absolute path + `cd` directive + reminder that pre-commit hook enforces this.
3. **Your instruction** — user's text verbatim in a blockquote.
4. **Task facts** — slug, branch, base branch, active_repo.
5. **Where the branch stands** — code block with aligned SHAs + inline human summary.
6. **Recent journal** — last 10 entries as a bulleted list; empty-state line if none.
7. **Conventions (recap)** — 3-bullet reminder + AGENTS.md path.
8. **Read these skills before starting** — canonical four with `<pkg>/skills/<name>/SKILL.md` paths; note that the subagent should use its platform's skill tool if available, else read directly.
9. **How to finish** — the finish contract (run `mship test`, write body, `mship finish --body-file`, return PR URL).

### Concrete example

````markdown
# Task: audit-split-severity-for-dirtyworktree-untracked-warn-modified-error-35

You are a subagent dispatched to work on an in-progress mothership task.

## Work from (mandatory)

Before editing anything: `cd /home/bailey/development/repos/mothership/.worktrees/feat/audit-split-severity-for-dirtyworktree-untracked-warn-modified-error-35`

This is a git worktree checked out on branch `feat/audit-split-severity-for-dirtyworktree-untracked-warn-modified-error-35`. Every edit, test run, and commit happens inside this directory. Do not edit from the main checkout — the mship pre-commit hook will refuse and you'll waste a cycle.

## Your instruction

> <verbatim user instruction>

## Task facts

- **slug:** audit-split-severity-for-dirtyworktree-untracked-warn-modified-error-35
- **branch:** feat/audit-split-severity-for-dirtyworktree-untracked-warn-modified-error-35
- **base branch:** main
- **active repo:** mothership

## Where the branch stands

```
base (main)       @ 338c43a
origin/main       @ 338c43a    (base is in sync with origin)
HEAD              @ 3b9f915    (6 commits ahead of base)
```

## Recent journal (last 10 entries)

- **2026-04-17T18:17:00Z** (iter=0, action="finished") — `mship finish` opened PR …
- **2026-04-17T18:06:52Z** (action="review-fix") — removed duplicate imports…
- *(… up to 10 entries …)*

*(When the task has zero journal entries, this section's bulleted list is replaced with a single italic line: "No entries yet — this task hasn't logged anything; your instruction above is the whole picture.")*

## Conventions (recap)

These are strictly enforced in this workspace:

- **Use `mship finish --body-file <path>` to open the PR.** Empty bodies are rejected by design. Write a real Summary and Test plan.
- **Don't edit from the main checkout.** Only the worktree path above. The pre-commit hook refuses otherwise.
- **Prefer `--bypass-<check>` over `--force-<check>`** on any mship command that takes one (e.g., `--bypass-reconcile`, `--bypass-audit`). Different flag name if you see `--force-<something>` in older docs; the bypass form is canonical.

Full doc: `/home/bailey/development/repos/mothership/AGENTS.md`.

## Read these skills before starting

Invoke via your platform's skill tool if it has one. Direct read paths (always valid; skills ship with mship):

- `working-with-mothership` — `<pkg>/skills/working-with-mothership/SKILL.md`
- `test-driven-development` — `<pkg>/skills/test-driven-development/SKILL.md`
- `finishing-a-development-branch` — `<pkg>/skills/finishing-a-development-branch/SKILL.md`
- `verification-before-completion` — `<pkg>/skills/verification-before-completion/SKILL.md`

## How to finish

When the work is done:

1. Run `mship test` until green (or confirm no test suite applies).
2. Write a PR body as a file — Summary + Test plan.
3. Run `mship finish --body-file <path>` in the worktree.
4. Return the PR URL in your final message.

If you get stuck or find the task is wrong-shaped, stop and report back with what you tried and where you're blocked. Don't guess.
````

### Voice and format notes

- Direct imperative voice ("Before editing anything: `cd …`"), not hedged ("Note: subagents should…"). Cold-start subagents respond to concrete directives.
- No YAML frontmatter. Pure markdown for widest framework consumability.
- "Where the branch stands" always emits — if there's no upstream, the `origin/<base>` line reads `origin/<base>       @ (no upstream)` and the summary reflects that.
- `<pkg>` is expanded at dispatch time to the resolved package path (same path `mship skill install` uses as its source).
- `agents_md_path` parameter to `build_dispatch_prompt` is `Path | None`. When `None` (rare — workspace has no `AGENTS.md`), the "Full doc:" line at the end of the conventions section is omitted. The 3-bullet recap still renders regardless.

## Testing

### Unit (`tests/core/test_dispatch.py`)

- `build_dispatch_prompt` with canned `Task` / fake journal / fake `BaseShaInfo` / fake paths → assert key substrings present in each section (worktree path, instruction verbatim, journal timestamps, skill paths, the 3 conventions).
- `resolve_repo` covering all four resolution-order cases (flag > active_repo > sole worktree > error).
- `collect_base_sha_info` with a real git fixture: fresh repo + one commit → `HEAD == base == origin/base`; add a local commit → ahead=1; add an upstream-only commit → behind=1; remove upstream → `summary` degrades to `no upstream tracked`.
- Empty journal → empty-case line renders, no crash.
- Canonical skill list is exactly the four expected names in expected order.

### CLI integration (`tests/cli/test_dispatch.py`)

- Single-repo task with cwd inside the worktree, no flags, `-i "do X"` → stdout contains worktree path + instruction verbatim + task slug; exit 0.
- Explicit `--task <slug>` from main checkout (no cwd resolution) → same output for that task.
- Multi-repo task with `active_repo` set → picks that repo's worktree; output contains its path.
- Multi-repo task with no `active_repo` and no `--repo` → exit 1, stderr lists the affected repos.
- `--repo <unknown>` → exit 1 with "unknown repo" message.
- `--task <unknown>` → exit 1 with "unknown task" message.
- Task with empty journal → prompt still emits cleanly.

### Manual smoke (called out in the plan, not gating unit CI)

- `uv run mship dispatch -i "implement the hello-world test"` from the current worktree; pipe to `claude -p "$(cat)"`; confirm the subagent `cd`s correctly and reads the worktree.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | CLI shape: `mship dispatch [--task] [--repo] -i "<text>"` with cwd-resolve default | Matches every other task-scoped mship command; zero-friction for the "subagent for the task I'm in" use case; `--task`/`--repo` as explicit overrides covers the cross-task / multi-repo cases. |
| 2 | `--instruction` / `-i` required (no default) | Keeps the command's intent unambiguous; YAGNI on a default. |
| 3 | Hardcoded canonical 4 skills (not config / scan / task-type) | The four apply to every task type; config adds surface for no v1 benefit; scan dilutes the signal. |
| 4 | Hardcoded 3-bullet AGENTS.md recap + path reference | Short recap keeps the prompt self-contained; path lets subagent round-trip for the long tail. Recap rarely changes. |
| 5 | Reference skills by name + package path | Agent-agnostic. Dispatching machine may have Claude; subagent may be Codex. The package path (`<pkg>/skills/<name>/SKILL.md`) is guaranteed to exist because skills ship with mship. |
| 6 | Multi-repo: one worktree per dispatch call | Composes cleanly with v2 multi-agent (N dispatches → N subagents). v1 supports multi-repo tasks via `--repo` flag or `active_repo`; errors out only when truly ambiguous. |
| 7 | Output: markdown only; no `--json` v1 | Markdown is the load-bearing primitive. JSON is a valid follow-up if a structured consumer appears. |
| 8 | No launcher wrapper (`--exec`) | Explicitly rejected in the issue body. Couples mship to each agent's CLI; forecloses tmux/matrix/SDK patterns. |
