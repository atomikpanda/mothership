# mship-skill workflow integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-04-17-mship-skill-integration-design.md`

**Goal:** Replace direct git/gh commands with mship verbs in four bundled skills (`finishing-a-development-branch`, `using-git-worktrees`, `writing-plans`, `executing-plans`) so agents following them route through mship's state-managing commands.

**Architecture:** Surgical edits to markdown files. No tests beyond a markdown-parse check + a post-install round-trip smoke. Structure and wording preserved outside the conflict sites per the spec's decision log.

**Tech Stack:** Markdown. `python -c "import mistune; ..."` for parse validation.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `src/mship/skills/finishing-a-development-branch/SKILL.md` | 5 inline edits: option label, Option 1 note, Option 2 fence, Option 4 fence, Step 5 body + table cell | modify |
| `src/mship/skills/using-git-worktrees/SKILL.md` | Top-of-skill callout + comment above line 97 + Example Workflow (lines 180-192) refresh | modify |
| `src/mship/skills/writing-plans/SKILL.md` | Extend Step 5 commit example to pair with `mship journal` | modify |
| `src/mship/skills/executing-plans/SKILL.md` | Add mship flow note to Step 3; promote Remember bullet to Step 1 sub-item | modify |

No test files change (skills are markdown). No code changes.

---

## Task 1: `finishing-a-development-branch` — five surgical sites

**File:** `src/mship/skills/finishing-a-development-branch/SKILL.md`

- [ ] **Step 1.1: Update Option 2's label (line 57)**

Change:

```markdown
2. Push and create a Pull Request
```

To:

```markdown
2. Finish the task and open a Pull Request
```

- [ ] **Step 1.2: Prepend mship-close note to Option 1 "Merge Locally" (before line 70)**

Insert the following italic note immediately after the `#### Option 1: Merge Locally` heading (line 68), before the existing fenced bash block:

```markdown
*In a mothership workspace, run `mship close` after the local merge to update state and clean up the worktree.*

```

(Blank line after the note, before the existing code fence.)

- [ ] **Step 1.3: Replace Option 2's fenced block (lines 91-104)**

Replace the existing fenced bash (from ``` ```bash ``` on line 91 through the closing ``` on line 104) with:

````markdown
```bash
# Write the PR body to a file (or pass inline via --body "...")
cat > /tmp/pr-body.md <<'EOF'
## Summary
<2-3 bullets of what changed>

## Test plan
- [ ] <verification steps>
EOF

# Finish the task: pushes the branch, opens the PR, stamps state.
mship finish --body-file /tmp/pr-body.md
```
````

- [ ] **Step 1.4: Replace Option 4's fenced block (lines 128-132)**

Replace the fenced bash block inside "If confirmed:":

````markdown
```bash
mship close --abandon
```
````

- [ ] **Step 1.5: Replace Step 5 "Cleanup Worktree" body (lines 136-148)**

Replace the whole section body (the "Check if in worktree" paragraph + both fenced blocks) between the `### Step 5: Cleanup Worktree` heading (line 136) and the next heading (`## Quick Reference`, line 152) with:

```markdown
**For Options 1, 2, 4:**

*In a mothership workspace, worktree cleanup is handled by `mship close` (run after merge for Option 1; run after merge notification for Option 2). No manual `git worktree remove` needed.*

**For Option 3:** Keep worktree.
```

- [ ] **Step 1.6: Update Quick Reference table cell (line 159)**

In the row for `4. Discard`, change the "Cleanup Branch" cell from `✓ (force)` to `✓ (via close --abandon)`. The row becomes:

```markdown
| 4. Discard | - | - | - | ✓ (via close --abandon) |
```

- [ ] **Step 1.7: Markdown parse validation**

Run:

```bash
uv run python -c "
import mistune, pathlib
p = pathlib.Path('src/mship/skills/finishing-a-development-branch/SKILL.md')
html = mistune.html(p.read_text())
assert '<h2' in html and '<code>' in html, 'lost structure'
print(f'OK — {len(html)} chars rendered')
"
```

Expected: `OK — <N> chars rendered`.

- [ ] **Step 1.8: Commit**

```bash
git add src/mship/skills/finishing-a-development-branch/SKILL.md
git commit -m "docs(skill/finishing): route Options 2 and 4 through mship verbs"
```

---

## Task 2: `using-git-worktrees` — top callout + inline comment + example refresh

**File:** `src/mship/skills/using-git-worktrees/SKILL.md`

- [ ] **Step 2.1: Add top-of-skill "In a mothership workspace" callout**

Insert the following new section between `## Overview` (ends at line 14) and `## Directory Selection Process` (line 16). The new section goes on lines 15–19 of the edited file (roughly):

```markdown
## In a mothership workspace

If a `mothership.yaml` is present at any ancestor directory, use `mship spawn '<description>'` instead of the steps below. `mship spawn` creates the worktree, registers it in workspace state, runs per-repo `task setup`, and (where configured) symlinks heavy directories. The Directory Selection, Safety Verification, and Creation Steps sections below apply only when you're not spawning a mship task (e.g., quick read-only exploration or a non-mship repo).

```

(Blank line before the next heading.)

- [ ] **Step 2.2: Add non-mship comment above `git worktree add` fence (around line 96)**

Immediately before the `### 2. Create Worktree` fenced block, insert a single sentence (one blank line before and after if needed):

```markdown
*For non-mship workflows, create the worktree directly:*
```

Then the existing fenced block starting with `# Determine full path` and containing `git worktree add "$path" -b "$BRANCH_NAME"` stays unchanged.

- [ ] **Step 2.3: Replace the Example Workflow block (lines 180-192)**

Replace the content between `## Example Workflow` (line 178) and `## Red Flags` (line 194) with:

````markdown
## Example Workflow

```
You: I'm using the using-git-worktrees skill to set up an isolated workspace.

[Detect: mothership.yaml found at /abs/workspace — routing through mship spawn]
[Run: mship spawn "implement auth middleware" --repos auth-service]
[mship creates worktree at .worktrees/feat/implement-auth-middleware, runs task setup]
[Run: mship test (baseline check)]

Worktree ready at /abs/workspace/.worktrees/feat/implement-auth-middleware
Tests passing (47 tests, 0 failures)
Ready to implement auth middleware
```
````

- [ ] **Step 2.4: Markdown parse validation**

```bash
uv run python -c "
import mistune, pathlib
p = pathlib.Path('src/mship/skills/using-git-worktrees/SKILL.md')
html = mistune.html(p.read_text())
assert '<h2' in html and '<code>' in html, 'lost structure'
assert 'mship spawn' in html, 'callout missing'
print(f'OK — {len(html)} chars rendered')
"
```

Expected: `OK — <N> chars rendered`.

- [ ] **Step 2.5: Commit**

```bash
git add src/mship/skills/using-git-worktrees/SKILL.md
git commit -m "docs(skill/worktrees): mship spawn callout + non-mship fallback framing"
```

---

## Task 3: `writing-plans` — pair commit example with `mship journal`

**File:** `src/mship/skills/writing-plans/SKILL.md`

- [ ] **Step 3.1: Extend Step 5 commit example (lines 98-103)**

Current content:

````markdown
- [ ] **Step 5: Commit**

```bash
git add tests/path/test.py src/path/file.py
git commit -m "feat: add specific feature"
```
````

Replace with:

````markdown
- [ ] **Step 5: Commit (pair with `mship journal` in a mothership workspace)**

```bash
git add tests/path/test.py src/path/file.py
git commit -m "feat: add specific feature"
# In a mothership workspace, also record the step in the journal:
mship journal "implemented specific feature; tests passing" --action committed
```

Pair the commit with a `mship journal` entry so other sessions can reconstruct progress without reading every commit diff.
````

- [ ] **Step 3.2: Markdown parse validation**

```bash
uv run python -c "
import mistune, pathlib
p = pathlib.Path('src/mship/skills/writing-plans/SKILL.md')
html = mistune.html(p.read_text())
assert 'mship journal' in html, 'journal pairing missing'
print(f'OK — {len(html)} chars rendered')
"
```

Expected: `OK — <N> chars rendered`.

- [ ] **Step 3.3: Commit**

```bash
git add src/mship/skills/writing-plans/SKILL.md
git commit -m "docs(skill/plans): pair commit example with mship journal entry"
```

---

## Task 4: `executing-plans` — mship flow note + promote Remember bullet

**File:** `src/mship/skills/executing-plans/SKILL.md`

- [ ] **Step 4.1: Extend Step 3 "Complete Development" (around lines 32-37)**

After the existing three bullets under `### Step 3: Complete Development`, append one more italic line:

```markdown
- **REQUIRED SUB-SKILL:** Use superpowers:finishing-a-development-branch
- Follow that skill to verify tests, present options, execute choice

*In a mothership workspace, `finishing-a-development-branch` routes through `mship finish --body-file <path>` (see that skill's Option 2).*
```

(Blank line between the last bullet and the italic note.)

- [ ] **Step 4.2: Promote the mship bullet to Step 1 (lines 18-23 and 63-69)**

Current Step 1:

```markdown
### Step 1: Load and Review Plan
1. Read plan file
2. Review critically - identify any questions or concerns about the plan
3. If concerns: Raise them with your human partner before starting
4. If no concerns: Create TodoWrite and proceed
```

Replace with:

```markdown
### Step 1: Load and Review Plan
1. Read plan file
2. Review critically — identify any questions or concerns about the plan
3. If concerns: Raise them with your human partner before starting
4. **If this is a mothership workspace** (`mothership.yaml` at any ancestor): verify `mship status` shows an active task BEFORE starting. No active task → stop and tell the user to `mship spawn "<description>"` first. Then `cd` into `task.worktrees.<repo>` and do all work and commits there. The mship pre-commit hook refuses commits from outside the worktree, so "just commit on main" is both wrong and blocked.
5. If no concerns: Create TodoWrite and proceed
```

Also remove the duplicated mship bullet from the "Remember" list at lines 64-69. The whole bullet starting with `- **If this is a mothership workspace**` through `both wrong and blocked.` goes away.

- [ ] **Step 4.3: Markdown parse validation**

```bash
uv run python -c "
import mistune, pathlib
p = pathlib.Path('src/mship/skills/executing-plans/SKILL.md')
html = mistune.html(p.read_text())
# Confirm Step 1 now contains the mship bullet
import re
step1_match = re.search(r'Load and Review Plan.*?(?=Step 2|## )', html, re.DOTALL)
assert step1_match and 'mship status' in step1_match.group(0), 'mship bullet not in Step 1'
# And that only ONE mship bullet remains total
assert html.count('mship spawn \"&lt;description&gt;\"') + html.count('mship spawn &quot;&lt;description&gt;&quot;') <= 2, 'duplication not removed'
print(f'OK — {len(html)} chars rendered')
"
```

Expected: `OK — <N> chars rendered`.

(The duplication assertion tolerates both HTML-entity forms that `mistune` may emit for the quoted string.)

- [ ] **Step 4.4: Commit**

```bash
git add src/mship/skills/executing-plans/SKILL.md
git commit -m "docs(skill/executing): surface mship gate in Step 1; note finish flow"
```

---

## Task 5: Install round-trip smoke

- [ ] **Step 5.1: Rebuild + reinstall the tool from this worktree**

```bash
uv tool install --reinstall --from . mothership 2>&1 | tail -3
mship skill install --force 2>&1 | tail -10
```

Expected: `claude: 15 skills installed → ~/.claude/skills/` (with some `refreshed` lines if prior install existed).

- [ ] **Step 5.2: Spot-check each edited skill via its installed path**

```bash
echo "--- finishing-a-development-branch ---"
grep -n "mship finish --body-file\|mship close --abandon" ~/.claude/skills/finishing-a-development-branch/SKILL.md | head -5

echo "--- using-git-worktrees ---"
grep -n "In a mothership workspace\|mship spawn" ~/.claude/skills/using-git-worktrees/SKILL.md | head -5

echo "--- writing-plans ---"
grep -n "mship journal" ~/.claude/skills/writing-plans/SKILL.md | head -3

echo "--- executing-plans ---"
grep -n "mship status\|finishing-a-development-branch.*mship finish" ~/.claude/skills/executing-plans/SKILL.md | head -5
```

Expected: each skill shows its mship mentions via the installed symlink.

- [ ] **Step 5.3: No commit needed (verification only).**

---

## Task 6: Finish

- [ ] **Step 6.1: Spec coverage check**

| Spec site | Task/step |
|---|---|
| 1.1 Option 2 label | 1.1 |
| 1.2 Option 1 mship-close note | 1.2 |
| 1.3 Option 2 fenced block | 1.3 |
| 1.4 Option 4 fenced block | 1.4 |
| 1.5 Step 5 body + table cell | 1.5, 1.6 |
| 2.1 Top-of-skill callout | 2.1 |
| 2.2 Line 97 non-mship comment | 2.2 |
| 2.3 Example Workflow refresh | 2.3 |
| 3.1 Commit example with journal | 3.1 |
| 4.1 Step 3 finish-flow note | 4.1 |
| 4.2 Promote mship bullet + remove duplicate | 4.2 |

- [ ] **Step 6.2: Full test suite (sanity — no code changed so this should be pure no-op green)**

```bash
uv run pytest -x -q 2>&1 | tail -3
```

Expected: all pass (no new tests, no changed Python).

- [ ] **Step 6.3: Finish via `mship finish --body-file -` (dogfood the flow the skills now describe)**

```bash
mship finish --body-file - <<'EOF'
## Summary

Surgical edits to four bundled skills so agents following them route through mship verbs instead of direct git/gh:

- **`finishing-a-development-branch`** — Option 2 now says `mship finish --body-file`; Option 4 says `mship close --abandon`; Step 5 notes that `mship close` cleans up the worktree (no manual `git worktree remove`).
- **`using-git-worktrees`** — Top-of-skill callout routes mship users to `mship spawn`; the `git worktree add` fence is flagged as the non-mship fallback; Example Workflow demonstrates the mship path.
- **`writing-plans`** — Step 5 commit example now pairs `git commit` with `mship journal --action committed`, showing plan authors the expected mship-plan pattern.
- **`executing-plans`** — Step 1 now surfaces the mship-workspace check before execution; Step 3 notes that the finish skill routes through `mship finish --body-file`.

Wording and structure preserved per the spec's "keep most as-is" directive. Ten other bundled skills are unchanged. No code changes; install round-trip smoke validates the edits flow through to `~/.claude/skills/`.

## Test plan

- [x] Markdown parse via `mistune.html()` for each edited file — all render without structural loss.
- [x] Install round-trip: `uv tool install --reinstall --from . mothership` then `mship skill install --force`; each skill's symlink target reflects the edits.
- [x] Full pytest (no code changed, pure sanity) green.
EOF
```
