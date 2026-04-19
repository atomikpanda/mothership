# Skills Audit — Multi-Task + Context/Dispatch + Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the top-ranked coverage gaps in the bundled skills — add multi-task parallelism + `mship context`/`mship dispatch` content to `working-with-mothership`, cross-reference from `subagent-driven-development`, rename `using-superpowers` → `using-mothership`, and drop the `superpowers:` namespace prefix from all cross-references. Add an installer legacy-sweep so users' stale `using-superpowers` symlinks get removed on upgrade.

**Architecture:** Content changes in two skills (`working-with-mothership`, `subagent-driven-development`). Mechanical directory rename + frontmatter + prose updates in `using-superpowers` → `using-mothership`. Global find-and-replace drops `superpowers:<name>` → `<name>` across 6 skill files. New `_RENAMED_SKILLS` constant + pre-install sweep in `skill_install.py` removes stale symlinks idempotently. Regression tests verify coverage and namespace cleanliness.

**Tech Stack:** Markdown skill files, Python 3.14, pytest, `grep`-based regression checks, `Path.unlink` for symlink removal.

**Reference spec:** `docs/superpowers/specs/2026-04-19-skills-audit-multi-task-context-dispatch-design.md`

---

## File structure

**Modified production files:**
- `src/mship/core/skill_install.py` — new `_RENAMED_SKILLS` constant + `_sweep_renamed_skills` helper called from `install_for_claude` before the main install loop.
- `src/mship/skills/working-with-mothership/SKILL.md` — two new sections added.
- `src/mship/skills/subagent-driven-development/SKILL.md` — one paragraph cross-reference added.
- `src/mship/skills/executing-plans/SKILL.md` — drop `superpowers:` prefix from 5 cross-references.
- `src/mship/skills/systematic-debugging/SKILL.md` — drop prefix from 3 references.
- `src/mship/skills/requesting-code-review/SKILL.md` — drop prefix from 3 references.
- `src/mship/skills/writing-plans/SKILL.md` — drop prefix from 3 references.
- `src/mship/skills/writing-skills/SKILL.md` — drop prefix from 4 references.
- `src/mship/skills/writing-skills/testing-skills-with-subagents.md` — drop prefix from 1 reference.
- `src/mship/skills/subagent-driven-development/code-quality-reviewer-prompt.md` — drop prefix from 1 reference.

**Renamed files:**
- `src/mship/skills/using-superpowers/` → `src/mship/skills/using-mothership/` (directory move).
  - Inside: `SKILL.md` frontmatter + body rewritten for mothership branding.
  - Inside: `references/copilot-tools.md` + `references/codex-tools.md` — drop `superpowers:` prefix from the agent-name examples.

**New test files:**
- `tests/core/test_skills_namespace.py` — regression tests for namespace cleanup + rename + content coverage.

**Unchanged:**
- All skills not listed above (`brainstorming`, `dispatching-parallel-agents`, `finishing-a-development-branch`, `receiving-code-review`, `test-driven-development`, `using-git-worktrees`, `using-superpowers` references themselves, `verification-before-completion`) — they don't contain `superpowers:` references in their SKILL.md.

**Task ordering rationale:**
- Task 1 (installer sweep) is independent — unit-testable without touching any skill file.
- Task 2 (rename) happens before the namespace cleanup so we don't re-touch moved files.
- Task 3 (namespace cleanup) is a separate mechanical pass that's easier to review on its own.
- Task 4 (content additions) is the substantive review target — isolate it from the mechanical churn.
- Task 5 (regression tests + smoke + PR) catches any gaps and ships.

---

## Task 1: Installer legacy-sweep for renamed skills

**Files:**
- Modify: `src/mship/core/skill_install.py`
- Modify: `tests/core/test_skill_install.py`

**Context:** Users who've previously run `mship skill install` have `~/.claude/skills/using-superpowers` symlinked at a package-owned path. After we rename the directory, that symlink dangles. Add a pre-install sweep that removes such stale symlinks IF and ONLY IF they're owned by mship (uses the existing `is_owned_target` check). Foreign or non-symlink entries are preserved.

This task lands BEFORE the rename so we can TDD the sweep without touching any skill file.

- [ ] **Step 1.1: Write failing tests**

Append to `tests/core/test_skill_install.py`:

```python
# --- Renamed-skills sweep (spec 2026-04-19) ---


def test_sweep_removes_stale_owned_symlink(tmp_path, monkeypatch):
    """An owned (mship-originated) symlink at a renamed location is removed."""
    import mship.core.skill_install as si

    # Fake home → tmp_path so `~/.claude/skills/...` lands in the sandbox.
    monkeypatch.setenv("HOME", str(tmp_path))

    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    pkg_src = si.pkg_skills_source()

    # Simulate a stale symlink: old name pointing at the package source (owned).
    stale = skills_dir / "using-superpowers"
    stale.symlink_to(pkg_src / "using-superpowers")  # may not exist on disk; dangling is OK

    # Test the rename-map directly (no need to install everything).
    si._sweep_renamed_skills(skills_dir, {"using-superpowers": "using-mothership"})

    assert not stale.exists() and not stale.is_symlink(), "stale symlink should be removed"


def test_sweep_preserves_foreign_symlink(tmp_path, monkeypatch):
    """A symlink at the renamed location pointing OUTSIDE mship's tree is left alone."""
    import mship.core.skill_install as si

    monkeypatch.setenv("HOME", str(tmp_path))
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)

    foreign_target = tmp_path / "elsewhere"
    foreign_target.mkdir()
    foreign = skills_dir / "using-superpowers"
    foreign.symlink_to(foreign_target)

    si._sweep_renamed_skills(skills_dir, {"using-superpowers": "using-mothership"})

    assert foreign.is_symlink(), "foreign symlink should be preserved"
    assert foreign.resolve() == foreign_target.resolve()


def test_sweep_preserves_regular_file(tmp_path, monkeypatch):
    """A regular file at the renamed location (user replaced the symlink) is preserved."""
    import mship.core.skill_install as si

    monkeypatch.setenv("HOME", str(tmp_path))
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)

    # Regular directory (user replaced symlink with their own content)
    (skills_dir / "using-superpowers").mkdir()
    (skills_dir / "using-superpowers" / "SKILL.md").write_text("my own version")

    si._sweep_renamed_skills(skills_dir, {"using-superpowers": "using-mothership"})

    assert (skills_dir / "using-superpowers").is_dir(), "regular directory should be preserved"


def test_sweep_noop_when_old_name_absent(tmp_path, monkeypatch):
    """No stale entry → no-op, no error."""
    import mship.core.skill_install as si

    monkeypatch.setenv("HOME", str(tmp_path))
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)

    # Nothing to sweep.
    si._sweep_renamed_skills(skills_dir, {"using-superpowers": "using-mothership"})


def test_install_for_claude_runs_sweep(tmp_path, monkeypatch):
    """install_for_claude invokes the sweep with the canonical _RENAMED_SKILLS map."""
    import mship.core.skill_install as si

    monkeypatch.setenv("HOME", str(tmp_path))
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)

    pkg_src = si.pkg_skills_source()
    stale = skills_dir / "using-superpowers"
    stale.symlink_to(pkg_src / "using-superpowers")

    si.install_for_claude(force=False)

    assert not stale.exists() and not stale.is_symlink()
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `pytest tests/core/test_skill_install.py -v -k sweep`

Expected: FAIL with `AttributeError: module 'mship.core.skill_install' has no attribute '_sweep_renamed_skills'` (and friends).

- [ ] **Step 1.3: Add the sweep helper + call site**

Edit `src/mship/core/skill_install.py`.

Near the top (after `HISTORICAL_SOURCES` and the `mship` import, before the first function), add:

```python
# Skills that have been renamed over mship's history. Keys are old skill
# names; values are the new names. On install, any stale owned symlink at
# the old name is removed so users don't carry dangling links.
_RENAMED_SKILLS: dict[str, str] = {
    "using-superpowers": "using-mothership",
}
```

Add the sweep helper — place it just before `def install_for_claude(...)`:

```python
def _sweep_renamed_skills(skills_dir: Path, rename_map: dict[str, str]) -> None:
    """Remove stale symlinks at renamed skill locations (mship-owned only).

    For each old-name → new-name entry: if ``skills_dir / old_name`` exists
    as an owned symlink (points inside a mship skills source), remove it.
    Leave foreign symlinks and real files/dirs untouched.
    """
    for old_name in rename_map:
        target = skills_dir / old_name
        if not target.is_symlink():
            continue  # regular file / dir / absent — leave alone
        intended = Path(os.readlink(target))
        if not intended.is_absolute():
            intended = (target.parent / intended).resolve(strict=False)
        if is_owned_target(intended):
            target.unlink()
```

Update `install_for_claude` to call the sweep before the per-skill loop:

Find:
```python
def install_for_claude(*, force: bool = False) -> AgentInstallResult:
    """Symlink each skill into ~/.claude/skills/<name>/."""
    src = pkg_skills_source()
    dest = Path.home() / ".claude" / "skills"
    skipped: list[str] = []
    replaced: list[str] = []
    skill_dirs = _iter_skill_dirs(src)
    for skill_dir in skill_dirs:
```

Insert the sweep call between `dest` and `skipped`:

```python
def install_for_claude(*, force: bool = False) -> AgentInstallResult:
    """Symlink each skill into ~/.claude/skills/<name>/."""
    src = pkg_skills_source()
    dest = Path.home() / ".claude" / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    _sweep_renamed_skills(dest, _RENAMED_SKILLS)
    skipped: list[str] = []
    replaced: list[str] = []
    skill_dirs = _iter_skill_dirs(src)
    for skill_dir in skill_dirs:
```

(The new `dest.mkdir(parents=True, exist_ok=True)` is defensive — previously `refresh_symlink` did this per-skill, but `_sweep_renamed_skills` runs before any `refresh_symlink`, and it needs `dest` to exist.)

The Codex installer (`install_for_codex`) uses a single dir-level symlink, not per-skill. No sweep needed there.

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `pytest tests/core/test_skill_install.py -v`
Expected: all tests pass (5 new + existing).

- [ ] **Step 1.5: Commit**

```bash
git add src/mship/core/skill_install.py tests/core/test_skill_install.py
git commit -m "feat(skill-install): legacy-sweep for renamed skills"
mship journal "installer pre-install sweep removes stale owned symlinks for renamed skills (using-superpowers → using-mothership)" --action committed
```

---

## Task 2: Rename `using-superpowers` → `using-mothership`

**Files:**
- Rename: `src/mship/skills/using-superpowers/` → `src/mship/skills/using-mothership/`
- Modify: `src/mship/skills/using-mothership/SKILL.md` (frontmatter + body)
- Modify: `src/mship/skills/using-mothership/references/copilot-tools.md`
- Modify: `src/mship/skills/using-mothership/references/codex-tools.md`

**Context:** Directory move + frontmatter update + prose re-branding. Mechanical; no architectural decisions.

- [ ] **Step 2.1: Move the directory**

```bash
git mv src/mship/skills/using-superpowers src/mship/skills/using-mothership
```

Verify with `git status` that the move is staged as a rename (not add+delete).

- [ ] **Step 2.2: Update the frontmatter in the renamed SKILL.md**

Edit `src/mship/skills/using-mothership/SKILL.md`. Find the frontmatter block at the top:

```markdown
---
name: using-superpowers
description: Use when starting any conversation - establishes how to find and use skills, requiring Skill tool invocation before ANY response including clarifying questions
---
```

Replace with:

```markdown
---
name: using-mothership
description: Use when starting any conversation — establishes how to find and use mothership-bundled skills, requiring Skill tool invocation before ANY response including clarifying questions
---
```

(Keeps the trigger scope as "any conversation" — matches the prior behavior. Only the branding changes: "skills" → "mothership-bundled skills".)

- [ ] **Step 2.3: Update the body prose**

Still in `src/mship/skills/using-mothership/SKILL.md`. Find the "Instruction Priority" section:

```markdown
## Instruction Priority

Superpowers skills override default system prompt behavior, but **user instructions always take precedence**:

1. **User's explicit instructions** (CLAUDE.md, GEMINI.md, AGENTS.md, direct requests) — highest priority
2. **Superpowers skills** — override default system behavior where they conflict
3. **Default system prompt** — lowest priority

If CLAUDE.md, GEMINI.md, or AGENTS.md says "don't use TDD" and a skill says "always use TDD," follow the user's instructions. The user is in control.
```

Replace with:

```markdown
## Instruction Priority

Mothership skills override default system prompt behavior, but **user instructions always take precedence**:

1. **User's explicit instructions** (CLAUDE.md, GEMINI.md, AGENTS.md, direct requests) — highest priority
2. **Mothership skills** — override default system behavior where they conflict
3. **Default system prompt** — lowest priority

If CLAUDE.md, GEMINI.md, or AGENTS.md says "don't use TDD" and a skill says "always use TDD," follow the user's instructions. The user is in control.
```

If there are other body references to "Superpowers" (capitalized as a proper noun referring to the fork), replace each with "Mothership". Lowercase "superpowers" (used as a feature-name) → "mothership". Run a grep within the file to catch stragglers:

```bash
grep -n -i "superpowers" src/mship/skills/using-mothership/SKILL.md
```

Each match should be reviewed; most are proper-noun references that should become "Mothership". Lowercase reference cases are unlikely but update if present.

- [ ] **Step 2.4: Update the references files**

Edit `src/mship/skills/using-mothership/references/copilot-tools.md`. Find the line with `superpowers:code-reviewer`:

```markdown
| Named plugin agents (e.g. `superpowers:code-reviewer`) | Discovered automatically from installed plugins |
```

Replace with:

```markdown
| Named plugin agents (e.g. `code-reviewer`) | Discovered automatically from installed plugins |
```

Edit `src/mship/skills/using-mothership/references/codex-tools.md`. Find the two `superpowers:code-reviewer` references:

```markdown
Claude Code skills reference named agent types like `superpowers:code-reviewer`.
```

and

```markdown
| `Task tool (superpowers:code-reviewer)` | `spawn_agent(agent_type="worker", message=...)` with `code-reviewer.md` content |
```

Replace both with:

```markdown
Claude Code skills reference named agent types like `code-reviewer`.
```

and

```markdown
| `Task tool (code-reviewer)` | `spawn_agent(agent_type="worker", message=...)` with `code-reviewer.md` content |
```

- [ ] **Step 2.5: Verify the rename is complete**

Run:

```bash
grep -rn "using-superpowers\|Superpowers skills\|name: using-superpowers" src/mship/skills/ 2>&1 | head -10
```

Expected: no matches.

Run:

```bash
ls src/mship/skills/using-mothership/
```

Expected: `SKILL.md`, `references/` (with `copilot-tools.md`, `codex-tools.md`).

- [ ] **Step 2.6: Commit**

```bash
git add -A src/mship/skills/using-mothership src/mship/skills/using-superpowers
git commit -m "feat(skills): rename using-superpowers → using-mothership"
mship journal "renamed the session-entry skill to match mothership branding; frontmatter + body prose updated; references/* rebranded" --action committed
```

---

## Task 3: Drop `superpowers:` prefix from cross-references

**Files:**
- Modify: `src/mship/skills/executing-plans/SKILL.md` (5 occurrences)
- Modify: `src/mship/skills/systematic-debugging/SKILL.md` (3 occurrences)
- Modify: `src/mship/skills/requesting-code-review/SKILL.md` (3 occurrences)
- Modify: `src/mship/skills/subagent-driven-development/SKILL.md` (7 occurrences)
- Modify: `src/mship/skills/subagent-driven-development/code-quality-reviewer-prompt.md` (1 occurrence)
- Modify: `src/mship/skills/writing-plans/SKILL.md` (3 occurrences)
- Modify: `src/mship/skills/writing-skills/SKILL.md` (4 occurrences)
- Modify: `src/mship/skills/writing-skills/testing-skills-with-subagents.md` (1 occurrence)

**Context:** Mechanical search-and-replace of `superpowers:<name>` → `<name>`. One pattern; applied per-file to make review diffs small and focused.

- [ ] **Step 3.1: Apply sed to each file**

Run this one-liner from the worktree root:

```bash
for f in \
  src/mship/skills/executing-plans/SKILL.md \
  src/mship/skills/systematic-debugging/SKILL.md \
  src/mship/skills/requesting-code-review/SKILL.md \
  src/mship/skills/subagent-driven-development/SKILL.md \
  src/mship/skills/subagent-driven-development/code-quality-reviewer-prompt.md \
  src/mship/skills/writing-plans/SKILL.md \
  src/mship/skills/writing-skills/SKILL.md \
  src/mship/skills/writing-skills/testing-skills-with-subagents.md; do
  sed -i 's/superpowers:\([a-zA-Z][a-zA-Z0-9_-]*\)/\1/g' "$f"
done
```

Notes on the regex:
- `superpowers:` followed by a name starting with an ASCII letter and containing letters, digits, underscores, or hyphens.
- Does NOT match `superpowers:` alone at end-of-line (no name).
- Preserves the name in the backreference.

- [ ] **Step 3.2: Verify zero matches remain**

Run:

```bash
grep -rn "superpowers:" src/mship/skills/ 2>&1
```

Expected: no output. If a match survived (e.g., escaped inside a code fence that confused the regex), edit it manually.

- [ ] **Step 3.3: Spot-check one file's diff**

Run:

```bash
git diff src/mship/skills/subagent-driven-development/SKILL.md | head -30
```

Expected: you see `-    "Use superpowers:finishing-a-development-branch"` / `+    "Use finishing-a-development-branch"` style pairs, no collateral damage.

- [ ] **Step 3.4: Commit**

```bash
git add src/mship/skills/
git commit -m "feat(skills): drop superpowers: prefix from cross-references"
mship journal "namespace cleanup — superpowers: prefix removed from all cross-references in skill files" --action committed
```

---

## Task 4: Content additions — multi-task + context/dispatch + cross-reference

**Files:**
- Modify: `src/mship/skills/working-with-mothership/SKILL.md` (two new sections)
- Modify: `src/mship/skills/subagent-driven-development/SKILL.md` (one paragraph added)

**Context:** Substantive writing, not mechanical. Both sections should feel consistent with the rest of the existing skill's voice (command examples, terse gotchas, typical-workflow patterns).

- [ ] **Step 4.1: Add "Working on multiple tasks at once" to working-with-mothership**

Edit `src/mship/skills/working-with-mothership/SKILL.md`. Find the end of the "Session Start Protocol" section (look for the section header and scroll past the contents until you reach the next top-level `##` heading).

Insert the new section after "Session Start Protocol" and before the next section:

```markdown
## Working on multiple tasks at once

Mothership supports multiple active tasks simultaneously. Typical reasons:

- Blocked on review of task A → start task B while waiting.
- Two unrelated investigations in flight; one can progress while the other is idle.
- Long-running `mship finish` / CI on task A while beginning new work on task B.

Each task has its own worktree at `.worktrees/feat/<slug>/`; they coexist cleanly.

### How mship resolves which task a command targets

In order of precedence:

1. **`--task <slug>` flag** — explicit; wins over everything else. Use for one-off commands from any pwd.
2. **`MSHIP_TASK=<slug>` env var** — shell/tab-level default. Useful when you want every `mship` command in a shell to target one task.
3. **cwd-based inference** — if pwd is inside `.worktrees/feat/<slug>/`, mship picks `<slug>`. This is the most ergonomic: `cd` into a worktree and every command defaults to that task.

Zero anchors (no `--task`, no `MSHIP_TASK`, pwd outside any worktree) + two active tasks → `AmbiguousTaskError` with a clear message listing the options.

### Typical parallel workflow

```bash
# Inventory active tasks:
mship worktrees

# Open a new terminal tab → enter a different task's worktree:
cd .worktrees/feat/other-task

# Every mship command in this tab now targets `other-task`:
mship status
mship journal "picked up work on other-task"

# Or, from any pwd, run a one-off against a specific task:
mship journal --task first-task "blocked on review; switching to other-task"

# Pin a whole script to a task:
MSHIP_TASK=other-task ./scripts/run-integration.sh
```

### Gotchas for multi-task

- `mship run` starts services. If two tasks each run services, ports will conflict unless task-scoped ports are configured in `mothership.yaml`.
- `mship sync` and `mship audit` operate on the workspace, not a single task — no `--task` needed (or honored).
- Resources outside mship's state (docker containers, DB tables, shared caches) are NOT task-scoped. Share where safe; tear down where not.

## Delegating to subagents: `mship context` and `mship dispatch`

Two mship-native primitives for handing work to subagents. Use them instead of hand-rolling task context:

- **`mship dispatch`** — emits a self-contained Markdown prompt for a subagent. Output includes task slug, worktree path, phase, recent journal entries, affected repos, and per-repo bases. Pipe stdout directly as the `prompt` field of a Claude Code `Task` tool dispatch (or analogous mechanism in Codex / other agent platforms).

  ```bash
  mship dispatch --task my-task   # prints a ready-to-use prompt to stdout
  ```

- **`mship context`** — emits structured JSON for programmatic consumers. Use when feeding state into a non-Claude-Code LLM, logging for audit, or scripting decisions. `jq`-friendly.

  ```bash
  mship context --task my-task | jq '.affected_repos'
  ```

### Decision tree

| You want to… | Use |
|---|---|
| Dispatch a Claude Code (or Codex) subagent to DO work | `mship dispatch` |
| Feed state into a different LLM / script / audit log | `mship context` |
| Know "which repos are in test phase?" | `mship context \| jq` |
| Generate an implementer prompt for a plan task | `mship dispatch` (see `subagent-driven-development`) |

Both accept `--task <slug>` for multi-task disambiguation. When executing a plan task-by-task, use `mship dispatch` as the basis for implementer prompts rather than hand-rolling the task context — it already knows the worktree path, slug, base branches, and recent journal.
```

- [ ] **Step 4.2: Add the cross-reference paragraph to subagent-driven-development**

Edit `src/mship/skills/subagent-driven-development/SKILL.md`. Find the "Example Workflow" section. Insert this paragraph immediately BEFORE the section's opening code fence (the `\`\`\`` that begins the workflow example):

```markdown
**Inside a mothership workspace:** prefer `mship dispatch --task <slug>` as the source of your implementer prompt's task context. It emits a self-contained Markdown block with the task slug, worktree path, phase, recent journal entries, and per-repo bases — handling multi-task disambiguation automatically. See `working-with-mothership` for the full decision tree on `mship dispatch` vs. `mship context`.
```

If the skill has no obvious "Example Workflow" header, place the paragraph immediately before the first `## Advantages` or similar post-process section — it's a pre-condition reminder, best placed as the transition from process description to examples.

- [ ] **Step 4.3: Spot-check the result**

Run:

```bash
grep -n "MSHIP_TASK\|mship dispatch\|mship context" src/mship/skills/working-with-mothership/SKILL.md | head -20
```

Expected: multiple hits for each. The document should read naturally around the new sections.

```bash
grep -n "mship dispatch" src/mship/skills/subagent-driven-development/SKILL.md | head -5
```

Expected: one hit (the new paragraph).

- [ ] **Step 4.4: Commit**

```bash
git add src/mship/skills/working-with-mothership/SKILL.md src/mship/skills/subagent-driven-development/SKILL.md
git commit -m "feat(skills): cover multi-task + mship dispatch/context; cross-ref from subagent-driven-development"
mship journal "working-with-mothership gains multi-task + dispatch/context sections; subagent-driven-development points at mship dispatch" --action committed
```

---

## Task 5: Regression tests + manual smoke + finish PR

**Files:**
- Create: `tests/core/test_skills_namespace.py`
- None else (verification only).

**Context:** Add a small regression-suite to catch namespace drift and verify the coverage gains don't regress. Then smoke the installer end-to-end and ship.

- [ ] **Step 5.1: Write the regression tests**

Create `tests/core/test_skills_namespace.py`:

```python
"""Regression tests for the 2026-04-19 skills audit:
- Namespace cleanup: no `superpowers:<name>` cross-references remain.
- Rename: `using-mothership/` exists with correct frontmatter; old dir gone.
- Coverage: `working-with-mothership` documents multi-task + dispatch/context.
"""
import subprocess
from pathlib import Path


SKILLS_DIR = Path(__file__).resolve().parents[2] / "src" / "mship" / "skills"


def test_no_superpowers_prefix_remains_in_skills():
    """No skill file cross-references `superpowers:<name>` anymore."""
    result = subprocess.run(
        ["grep", "-rn", "superpowers:", str(SKILLS_DIR)],
        capture_output=True, text=True,
    )
    # grep exit codes: 0 = matches, 1 = no matches, 2 = error.
    # We want 1 (no matches).
    assert result.returncode == 1, f"Found leftover superpowers: references:\n{result.stdout}"


def test_using_mothership_directory_exists():
    assert (SKILLS_DIR / "using-mothership").is_dir()
    assert (SKILLS_DIR / "using-mothership" / "SKILL.md").is_file()


def test_using_superpowers_directory_is_gone():
    assert not (SKILLS_DIR / "using-superpowers").exists()


def test_using_mothership_frontmatter_name_is_correct():
    content = (SKILLS_DIR / "using-mothership" / "SKILL.md").read_text()
    assert "name: using-mothership" in content
    assert "name: using-superpowers" not in content


def test_working_with_mothership_covers_multi_task():
    content = (SKILLS_DIR / "working-with-mothership" / "SKILL.md").read_text()
    assert "MSHIP_TASK" in content, "multi-task env var must be documented"
    assert "mship worktrees" in content, "worktrees command should appear in multi-task section"
    assert "multiple tasks" in content.lower(), "Section header or prose should mention multi-task"


def test_working_with_mothership_covers_dispatch_and_context():
    content = (SKILLS_DIR / "working-with-mothership" / "SKILL.md").read_text()
    assert "mship dispatch" in content
    assert "mship context" in content


def test_subagent_driven_development_cross_refs_mship_dispatch():
    content = (SKILLS_DIR / "subagent-driven-development" / "SKILL.md").read_text()
    assert "mship dispatch" in content, "subagent-driven-development should mention mship dispatch"
```

- [ ] **Step 5.2: Run tests**

Run: `pytest tests/core/test_skills_namespace.py -v`
Expected: 7 passed.

If any fail, the earlier tasks missed something — diagnose and fix the earlier commit rather than the test.

- [ ] **Step 5.3: Run the full test suite**

Run: `pytest tests/ 2>&1 | tail -5`
Expected: all pass (~910+ with the new tests).

- [ ] **Step 5.4: Reinstall tool**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/skills-audit-feature-coverage-multi-task-flows-context-and-dispatch
uv tool install --reinstall --from . mothership
```

- [ ] **Step 5.5: Smoke the installer**

Before running, pre-create a stale symlink to simulate the upgrade path:

```bash
ln -sfn $HOME/.local/share/uv/tools/mothership/lib/python3.14/site-packages/mship/skills/using-superpowers $HOME/.claude/skills/using-superpowers
ls -la $HOME/.claude/skills/using-superpowers
```

(The path to the package's skills/ dir can be verified via `python3 -c "import mship, os; print(os.path.dirname(mship.__file__) + '/skills')"` if needed.)

Now run the installer:

```bash
mship skill install
```

Verify:

```bash
ls -la $HOME/.claude/skills/ | grep -E "using-(mothership|superpowers)"
```

Expected:
- `using-mothership` symlink exists (→ `…/src/mship/skills/using-mothership`).
- `using-superpowers` is GONE.

- [ ] **Step 5.6: Open the PR**

Write to `/tmp/skills-audit-body.md`:

```markdown
## Summary

Skills audit — first fix out of a prioritized backlog. Closes the top-ranked gaps found by surveying all 15 bundled skills: `mship context` and `mship dispatch` had zero mentions, multi-task parallelism (MSHIP_TASK / --task / cwd resolution) was undocumented, and the entry-point skill was still branded `using-superpowers`.

## Coverage gains

- `working-with-mothership` gains two new sections:
  - **Working on multiple tasks at once** — three resolution mechanisms (`--task`, `MSHIP_TASK`, cwd-based), typical workflow, gotchas (ports, docker state, shared resources).
  - **Delegating to subagents: `mship context` and `mship dispatch`** — what each emits, decision tree for which to use, multi-task disambiguation via `--task`.
- `subagent-driven-development` gets a one-paragraph pointer: inside a mothership workspace, use `mship dispatch --task <slug>` as the source of implementer prompt context.

## Rename + namespace cleanup

- `using-superpowers` skill renamed to `using-mothership` (directory + frontmatter + body prose + references/).
- `superpowers:<name>` prefix dropped from cross-references across 8 skill files (executing-plans, systematic-debugging, requesting-code-review, subagent-driven-development, writing-plans, writing-skills, and two supporting files).
- `skill_install.py` gains a `_RENAMED_SKILLS` map + `_sweep_renamed_skills` helper called from `install_for_claude` — users running `mship skill install` after upgrading will have their stale `using-superpowers` symlink removed automatically.

## Scope boundaries

- No new skills created.
- No restructure of existing skills; only additive content edits.
- Skills not listed above are untouched (`brainstorming`, `dispatching-parallel-agents`, `finishing-a-development-branch`, `receiving-code-review`, `test-driven-development`, `using-git-worktrees`, `verification-before-completion` — these didn't contain `superpowers:` references in their SKILL.md).
- `mship context` / `mship dispatch` behavior unchanged — documenting existing commands.
- External user docs referencing `superpowers:<name>` will need manual migration — out of scope.

## Test plan

- [x] `tests/core/test_skill_install.py`: 5 new tests covering the legacy-sweep helper (owned symlink removed, foreign preserved, regular file preserved, no-op when absent, end-to-end via `install_for_claude`).
- [x] `tests/core/test_skills_namespace.py` (new): 7 regression tests (no `superpowers:` prefix anywhere, old dir gone, new dir with correct frontmatter, `working-with-mothership` covers MSHIP_TASK + worktrees + multi-task + dispatch + context, `subagent-driven-development` references `mship dispatch`).
- [x] Full suite: all pass.
- [x] Manual smoke: pre-create stale `using-superpowers` symlink → `mship skill install` → confirm removed + `using-mothership` symlink created.

Part of the larger skills-audit backlog; remaining gaps (low-coverage commands like `view`/`layout`, multi-skill integrations) will be filed as follow-up issues.
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/skills-audit-feature-coverage-multi-task-flows-context-and-dispatch
mship finish --body-file /tmp/skills-audit-body.md
```

Expected: PR URL returned.

---

## Done when

- [x] `_RENAMED_SKILLS` constant + `_sweep_renamed_skills` helper land in `skill_install.py`; `install_for_claude` calls the sweep before per-skill install.
- [x] `src/mship/skills/using-superpowers/` renamed to `src/mship/skills/using-mothership/`. Frontmatter, body prose, and references/* updated.
- [x] `superpowers:<name>` prefix dropped from all cross-references in skill files.
- [x] `working-with-mothership` gains "Working on multiple tasks at once" and "Delegating to subagents: `mship context` and `mship dispatch`" sections.
- [x] `subagent-driven-development` cross-references `mship dispatch`.
- [x] Regression tests (`test_skills_namespace.py` + new tests in `test_skill_install.py`) all pass.
- [x] Manual smoke confirms the legacy-sweep removes a pre-existing stale symlink.
- [x] Full pytest green.
