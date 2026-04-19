# Skills audit — multi-task flows, context/dispatch coverage, rename + namespace cleanup — Design

## Context

Bailey surfaced this during a real session retrospective: "our skills in real world usage... don't tell the full story on how to use mothership. Particularly around how multiple mothership tasks can be spawned in parallel. I don't know if people even use `mship context` or `mship dispatch` at all or if all of mship's features are even mentioned in the skills."

An audit of the 15 bundled skills confirmed the concern with numbers:

| Skill | mship mentions | Commands covered |
|---|---:|---|
| `working-with-mothership` | 68 | 12 commands (status, spawn, finish, close, test, run, journal, switch, audit, doctor, view, phase) |
| `using-git-worktrees` | 6 | spawn, close, test |
| `finishing-a-development-branch` | 6 | finish, close, reconcile |
| `subagent-driven-development` | 3 | status, spawn |
| `executing-plans` | 3 | status, spawn, finish |
| `writing-plans` | 3 | journal |
| 9 other skills | 0 | none |

**Zero mentions across ALL 15 skills:** `mship context`, `mship dispatch`, `mship layout`, `mship graph`, `mship worktrees`, `mship logs`, `mship prune`, `mship sync`, `mship reconcile` (in most), `mship block`/`unblock`, `mship switch` (in most), `mship init`, `mship skill`.

**Top-ranked gaps by user impact:**

1. **`mship context` + `mship dispatch` are invisible.** They're the mship-native agent-delegation primitives. Agents reinvent ad-hoc state-passing and hand-rolled subagent prompts because the bundled skills never tell them these tools exist.
2. **Multi-task parallelism is undocumented.** `MSHIP_TASK` env var, `--task` flag, cwd-based resolution, and how 2+ active tasks coexist — none of it appears in any skill.

Plus a separate branding issue: the entry-point skill is still named `using-superpowers` (inherited from a fork of the Superpowers plugin). `superpowers:`-prefixed cross-references appear in 10 other skills. This is confusing for a tool branded as mothership.

This spec covers both categories — content gap fixes (items 1 + 2) and the rename/namespace cleanup — in one PR. They land together cleanly because the rename is mechanical and the content additions are the substantive review target.

## Goal

1. Extend `working-with-mothership` to cover **multi-task parallelism** (resolving tasks via `--task` / `MSHIP_TASK` / cwd, managing 2+ active tasks) and **subagent delegation** (`mship context` vs. `mship dispatch`, decision tree, multi-task scoping).
2. Add a cross-reference from `subagent-driven-development` to `mship dispatch` as the native prompt generator inside mothership workspaces.
3. Rename `using-superpowers` skill → `using-mothership`; update content to match.
4. Drop the `superpowers:` prefix from all cross-references in all skills.

## Success criterion

**Coverage:**
- `grep -rn "mship context" src/mship/skills/` returns ≥ 1 match (currently 0).
- `grep -rn "mship dispatch" src/mship/skills/` returns ≥ 1 match (currently 0).
- `grep -rn "MSHIP_TASK" src/mship/skills/` returns ≥ 2 matches (in `working-with-mothership`'s new multi-task section).
- `grep -rn "superpowers:" src/mship/skills/` returns 0 matches.

**Branding:**
- `src/mship/skills/using-superpowers/` does not exist.
- `src/mship/skills/using-mothership/` exists with `name: using-mothership` in its frontmatter and mship-branded body text.

**Behavior:**
- `mship skill install` installs the new `using-mothership` symlink AND removes any stale `using-superpowers` symlink left behind by the rename.
- Existing agents that invoke `Skill(skill="subagent-driven-development")` (bare name) continue to work — no prefix change to how skills are invoked, only how they cross-reference each other in prose.

## Anti-goals

- **No restructure.** The 15-skill layout stays. No new skills created or split.
- **No content-rewrite of skills outside the explicitly-named ones.** `test-driven-development`, `systematic-debugging`, etc. get the namespace-cleanup find-and-replace but no prose changes.
- **No new mship commands.** `mship context` and `mship dispatch` already exist; we're only documenting them.
- **No dependency-ordering logic changes** for multi-task. We're describing how multi-task works today, not re-architecting it.
- **No `mship context` / `mship dispatch` behavior changes** (flags, output format). Documenting the current interface.
- **No automated migration for user-written docs** that reference `superpowers:<name>`. That's external to this repo.
- **No comprehensive command-reference** in the skill. Commands get covered contextually within the flows that need them. A full command reference is a separate doc/skill.
- **No removal of commands from `working-with-mothership`** that are already there. Only additive.

## Architecture

### Content additions to `src/mship/skills/working-with-mothership/SKILL.md`

Two new sections. Placement: after the existing "Session Start Protocol" section, before the first phase-transition or workflow-coordination section. Insertion point is self-contained — no rewrites of the surrounding material.

#### Section 1: "Working on multiple tasks at once"

Purpose: document the three task-resolution mechanisms and the typical multi-task workflow. Structure:

- **Why you'd want this** — blocked on review of task A → start task B; two independent issues in parallel; finish pipeline running on one while developing on another.
- **Three resolution mechanisms** (in order of precedence):
  1. `--task <slug>` flag — explicit; wins over everything else.
  2. `MSHIP_TASK=<slug>` env var — shell/tab-level default.
  3. cwd-based inference — if pwd is inside `.worktrees/feat/<slug>`, mship picks `<slug>`.
- **Typical workflow** — pattern for working two tasks in parallel:
  - `mship worktrees` to inventory all active tasks.
  - Open a new terminal tab → `cd .worktrees/feat/<other-slug>` → every `mship` command in that tab targets the other task automatically.
  - One-off from any pwd: `mship <cmd> --task <slug>`.
  - Session-level pinning: `export MSHIP_TASK=<slug>` at the top of a script or shell rc.
- **Gotchas** — concrete sharp edges:
  - `mship run` starts services. Two tasks' `run` will conflict on ports unless task-scoped ports are configured in `mothership.yaml`.
  - `mship sync` and `mship audit` operate on the workspace, not a single task — no `--task` needed.
  - Resources outside mship's state (docker containers, DB tables) are NOT task-scoped. Share where safe; tear down where not.

#### Section 2: "Delegating to subagents: `mship context` and `mship dispatch`"

Purpose: introduce the two agent-facing primitives and the decision tree for which to use.

- **`mship dispatch`** — emits a self-contained markdown prompt for a subagent. Output includes task slug, worktree path, phase, recent journal entries, affected repos, and per-repo base branches. Pipe stdout directly as the `prompt` field of a Claude Code `Task` tool dispatch (or analogous subagent mechanism).
- **`mship context`** — emits JSON state snapshot for programmatic consumers. Use when feeding state into a non-Claude-Code LLM, logging for audit, or scripting decisions (e.g., "which repos are in test phase?"). `jq`-friendly structure.
- **Decision tree:**
  - Dispatching a Claude Code (or Codex) subagent to do work → `mship dispatch`.
  - Need structured state for logs / scripts / other tools → `mship context`.
  - Both accept `--task <slug>` for multi-task disambiguation.
- **Multi-task scoping:** in a session with two active tasks, `mship dispatch --task <slug>` produces a prompt scoped to `<slug>`'s worktree. Subagents dispatched this way work in isolation and don't need to know about the sibling task.
- **Integration with `subagent-driven-development`:** when executing a plan task-by-task with that skill, prefer `mship dispatch` as the basis for implementer prompts rather than hand-rolling task context. The cross-reference in `subagent-driven-development` points back here.

Both sections include small runnable examples (command + sample output shape). No new CLI is introduced; these are pure doc additions.

### Cross-reference update in `src/mship/skills/subagent-driven-development/SKILL.md`

One paragraph inserted near the "Example Workflow" section (before the "Advantages" block). Exact text to add:

> **Inside a mothership workspace:** prefer `mship dispatch --task <slug>` as the source of your implementer prompt's task context. It emits a self-contained markdown block with the task slug, worktree path, phase, recent journal entries, and per-repo bases — handling multi-task disambiguation automatically. See `working-with-mothership` for the full decision tree on `mship dispatch` vs. `mship context`.

### Rename: `using-superpowers` → `using-mothership`

**File-level changes:**
- Directory move: `src/mship/skills/using-superpowers/` → `src/mship/skills/using-mothership/`.
- All files under the directory preserved (`SKILL.md`, `references/copilot-tools.md`, `references/codex-tools.md`).

**Content updates inside `SKILL.md`:**
- Frontmatter: `name: using-superpowers` → `name: using-mothership`.
- Frontmatter: description rewritten to "Use when starting any conversation in a mothership workspace — establishes how to find and use mothership-bundled skills, requiring Skill tool invocation before ANY response including clarifying questions."
- Body prose: rename "Superpowers skills" → "Mothership skills"; "superpowers" (lowercase) → "mothership" where used as branding. Non-branding uses (e.g., function names, historical references) are preserved.
- Section title "Superpowers skills override default system prompt behavior" → "Mothership skills override default system prompt behavior".
- Section "Superpowers skills" in the priority list → "Mothership skills".

**Reference files inside `references/`:**
- `copilot-tools.md`: replace `superpowers:code-reviewer` → `code-reviewer` (namespace cleanup).
- `codex-tools.md`: same treatment.

### Namespace cleanup: drop `superpowers:` globally

Find-and-replace (`superpowers:<name>` → `<name>`) across all skills:

- `executing-plans/SKILL.md` — 4 references (subagent-driven-development, finishing-a-development-branch, using-git-worktrees, writing-plans, finishing-a-development-branch again).
- `subagent-driven-development/SKILL.md` — 7 references (finishing-a-development-branch × 2, using-git-worktrees, writing-plans, requesting-code-review, finishing-a-development-branch again, test-driven-development, executing-plans).
- `systematic-debugging/SKILL.md` — 3 references (test-driven-development × 2, verification-before-completion).
- `using-mothership/references/*` — as noted above.
- Any other skill file that surfaces a `superpowers:<name>` reference after this change lands.

Verification: `grep -rn "superpowers:" src/mship/skills/` returns 0 matches post-change (we accept the match being absent even from historical sections; no decision-log or changelog currently references it by name in the skill files themselves).

### Installer behavior: stale `using-superpowers` symlink cleanup

`src/mship/core/skill_install.py` installs per-skill symlinks into `~/.claude/skills/<name>`. After this rename, users who previously ran `mship skill install` have a stale `using-superpowers` symlink pointing at a nonexistent directory.

Add a small "legacy sweep" step to the installer:
- Maintain a module-level constant `_RENAMED_SKILLS: dict[str, str] = {"using-superpowers": "using-mothership"}` — old-name to new-name map.
- Before installing new symlinks, iterate `_RENAMED_SKILLS` keys. For each, if `~/.claude/skills/<old-name>` exists AND is owned by mship (via existing `is_owned_target` check), remove it. Leave user-managed non-symlinks or foreign-owned links untouched.
- Same treatment in the Codex install path (`~/.agents/skills/mothership/<old-name>` → cleanup).

This keeps the migration one-shot: `mship skill install` after upgrading removes the stale link and creates the new one.

## Data flow

**Agent in a fresh session (post-change):**
1. Agent loads available skills. Sees `using-mothership` in the list.
2. `using-mothership` SKILL.md triggers at session start (same trigger phrase as before — just renamed).
3. Agent reads `working-with-mothership` for mship-specific flows. Discovers multi-task and `mship dispatch` sections.
4. Later, executing a plan task-by-task via `subagent-driven-development`, agent sees the cross-ref → dispatches subagent prompts generated by `mship dispatch`.

**User running `mship skill install` after upgrading:**
1. Installer reads `_RENAMED_SKILLS` mapping.
2. For each old-name, checks `~/.claude/skills/<old-name>`; removes if mship-owned.
3. Installs fresh `using-mothership` symlink.
4. Doctor run (`mship doctor`) confirms no dangling links.

**Agent invoking a skill by name (unchanged):**
- `Skill(skill="subagent-driven-development")` — works identically before and after. Prefix cleanup only affects prose cross-references; the skill tool never needed the prefix.

## Error handling

- **Rename collision:** user has a hand-written `~/.claude/skills/using-mothership` (implausible since the name didn't exist before). Existing `is_owned_target` check prevents overwriting; installer surfaces a clear error.
- **`using-superpowers` is a regular file, not a symlink** (extremely unusual — someone replaced the symlink with content): installer leaves it alone. Doctor reports it under the existing `foreign` detection.
- **User's personal docs reference `superpowers:<name>`:** can't be migrated by this PR. One-line note in the PR body asks users to find-and-replace locally if they have such docs.
- **Namespace-cleanup misses a reference:** `grep -rn "superpowers:" src/mship/skills/` run during PR review catches any stragglers.

## Testing

### Unit — `tests/core/test_skill_install.py` (extend)

1. **Rename sweep removes stale symlink.** Pre-create a symlink at `tmp_home/.claude/skills/using-superpowers` pointing inside a mship-owned package path. Run installer. Assert symlink is gone after install.
2. **Rename sweep doesn't touch foreign symlinks.** Pre-create `tmp_home/.claude/skills/using-superpowers` pointing at an external non-mship path. Run installer. Assert symlink is preserved (ownership check protects it).
3. **Rename sweep doesn't touch non-symlink files.** Pre-create `tmp_home/.claude/skills/using-superpowers` as a regular file. Run installer. Assert file is preserved.
4. **Fresh install with no legacy state.** No `using-superpowers` entry exists. Run installer. Assert `using-mothership` symlink created at expected location, no errors.
5. **Codex path gets the same sweep.** Repeat scenario 1 against `~/.agents/skills/mothership/using-superpowers`.

### Unit — audit verification via grep

A small test that runs as part of the suite:

```python
# tests/core/test_skills_namespace.py (new, tiny)
import subprocess
from pathlib import Path

SKILLS_DIR = Path(__file__).parent.parent.parent / "src" / "mship" / "skills"


def test_no_superpowers_prefix_remains():
    """Namespace cleanup: no skill files reference `superpowers:<name>`."""
    result = subprocess.run(
        ["grep", "-rn", "superpowers:", str(SKILLS_DIR)],
        capture_output=True, text=True,
    )
    # grep returns 1 on zero matches, 0 on matches found; we want NO matches.
    assert result.returncode == 1, f"Found leftover superpowers: references:\n{result.stdout}"


def test_multi_task_section_present():
    """working-with-mothership covers MSHIP_TASK, mship dispatch, mship context."""
    content = (SKILLS_DIR / "working-with-mothership" / "SKILL.md").read_text()
    assert "MSHIP_TASK" in content
    assert "mship dispatch" in content
    assert "mship context" in content


def test_using_mothership_skill_exists_with_correct_name():
    skill_md = SKILLS_DIR / "using-mothership" / "SKILL.md"
    assert skill_md.is_file()
    content = skill_md.read_text()
    assert "name: using-mothership" in content
    old_skill = SKILLS_DIR / "using-superpowers"
    assert not old_skill.exists()
```

Runs on every `pytest`. Catches accidental regressions in future edits.

### Regression

- All existing skill-install tests stay green (installer change is additive — new pre-step, existing logic unchanged).
- Full `pytest tests/` stays green.

### No manual smoke beyond self-install

After the change lands, `mship skill install` from the feature branch's worktree should produce:
- `~/.claude/skills/using-mothership` symlink (new).
- `~/.claude/skills/using-superpowers` removed (if present).
- All other skill symlinks unchanged.

Quick check: `ls -la ~/.claude/skills/` should show `using-mothership → …/src/mship/skills/using-mothership` and no `using-superpowers` entry.

## Decisions log

| # | Decision | Rationale |
|---|---|---|
| 1 | Bundle content additions + rename + namespace cleanup into one PR | The rename/cleanup is mechanical; the content is the substantive review target. Splitting them would produce a PR that's 90% find-and-replace plus a second PR with content. Together they tell one story: "make the bundled skills mship-branded and feature-complete." |
| 2 | Cover multi-task + context/dispatch in `working-with-mothership`, not new skills | Adding new skills dilutes discoverability. The existing skill already has 348 lines; adding ~100 for two focused sections keeps the surface area manageable without fragmenting. |
| 3 | Drop `superpowers:` prefix globally, don't replace with `mothership:` | Skills are invoked by bare name via the Skill tool; the prefix was always documentation-only. Dropping it matches how skills actually work in practice and avoids locking in another namespace we might want to change later. |
| 4 | Rename the installer has a "legacy sweep" for old skill names | Users shouldn't have to manually `rm ~/.claude/skills/using-superpowers` after an upgrade. Keeping the sweep generic (dict-based) lets us handle future renames the same way. |
| 5 | Cross-reference `subagent-driven-development` → `working-with-mothership`, not inline | A one-paragraph pointer is enough. The full decision tree belongs in one place (`working-with-mothership`), not duplicated. |
| 6 | No command reference; keep contextual coverage | A comprehensive `mship` command reference is a separate doc concern. Skills should cover flows, not be man-pages. Low-coverage commands (view, layout, graph) are fine at their current depth in `working-with-mothership`. |
| 7 | Only touch skills in the rename/cleanup scope that actually contain `superpowers:` references | Many skills don't reference the prefix at all. Leaving them untouched keeps the diff focused and review-able. |
| 8 | Add a grep-based regression test for the namespace cleanup | Cheapest way to catch future drift. Without a test, `superpowers:` will creep back in during unrelated edits. |
