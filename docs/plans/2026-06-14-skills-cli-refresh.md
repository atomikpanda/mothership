# Skills ↔ CLI Refresh Implementation Plan (Tier 2/3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the mship-bundled skills up to date with the current CLI — weave the `mship spec` lifecycle into the workflow skills (Approach A: one canonical description in `working-with-mothership`, thin cross-references elsewhere), fold in the Tier 3 coverage gaps, and add a configurable `docs_dir`.

**Architecture:** One small additive code change (`docs_dir` on `WorkspaceConfig` + surfaced in `mship context`); the rest are documentation edits across the bundled skills under `src/mship/skills/`. A final guard test sweeps the edited skills for stale references and asserts the spec-lifecycle commands named in the docs actually exist in the CLI.

**Tech Stack:** Python, pydantic v2 (`WorkspaceConfig`), Typer, pytest, `uv`. Skills are Markdown (`SKILL.md`).

**Design:** [docs/specs/2026-06-14-skills-cli-refresh-design.md](../specs/2026-06-14-skills-cli-refresh-design.md)

**Base note:** This branch must be spawned AFTER [#175](https://github.com/atomikpanda/mothership/pull/175) (Tier 1) merges — it edits the same skill files. Spawn from an updated `main`.

---

## File structure

- **Modify** `src/mship/core/config.py` — add `docs_dir` field to `WorkspaceConfig`.
- **Modify** `src/mship/cli/context.py` — surface `docs_dir` in the payload.
- **Test** `tests/core/test_config.py`, `tests/cli/test_context.py` — config + context.
- **Modify** `src/mship/skills/working-with-mothership/SKILL.md` — canonical Specs section + Tier 3 (serve/debug/finish).
- **Modify** `src/mship/skills/brainstorming/SKILL.md` — dual-path output → `mship spec`.
- **Modify** `src/mship/skills/{writing-plans,executing-plans,subagent-driven-development}/SKILL.md` — thin spec cross-refs.
- **Modify** `src/mship/skills/{using-mothership,writing-skills}/SKILL.md` — `mship skill` coverage.
- **Modify** `src/mship/skills/{test-driven-development,verification-before-completion,dispatching-parallel-agents}/SKILL.md` — evidence pipeline + worktree-awareness.
- **Modify** `src/mship/skills/using-git-worktrees/SKILL.md` — drop the `~/.config/superpowers/` global fallback.
- **Test** `tests/core/test_skills_cli_refresh.py` (new) — the guard test.

---

## Task 1: `docs_dir` config + `mship context` surfacing

**Files:**
- Modify: `src/mship/core/config.py` (in `WorkspaceConfig`, after `require_approved_spec` ~line 118)
- Modify: `src/mship/cli/context.py` (after the `build_context` call, ~line 43)
- Test: `tests/core/test_config.py`, `tests/cli/test_context.py`

- [ ] **Step 1: Write the failing config tests.** Append to `tests/core/test_config.py` (they reuse the existing `workspace` fixture + `ConfigLoader.load`, exactly like `test_default_branch_pattern` / `test_custom_branch_pattern`):

```python
def test_docs_dir_defaults_to_docs(workspace: Path):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    assert config.docs_dir == "docs"


def test_custom_docs_dir(workspace: Path):
    cfg = workspace / "mothership.yaml"
    cfg.write_text(cfg.read_text() + 'docs_dir: "documentation"\n')
    config = ConfigLoader.load(cfg)
    assert config.docs_dir == "documentation"
```

- [ ] **Step 2: Run → fail.** `uv run pytest tests/core/test_config.py -k docs_dir -v` → FAIL (`WorkspaceConfig` has no `docs_dir`).

- [ ] **Step 3: Add the field.** In `src/mship/core/config.py`, inside `WorkspaceConfig`, immediately after the `require_approved_spec` field (~line 118) and before `repos`:

```python
    # Workspace-relative directory where the bundled skills write plan docs (and,
    # outside a workspace, fallback design docs). Plans live at `<docs_dir>/plans/`.
    # Does NOT affect canonical mship specs (always `specs/`) or the `spec_paths`
    # legacy spec-search default. Surfaced in `mship context` for skills.
    docs_dir: str = "docs"
```

- [ ] **Step 4: Run → pass.** `uv run pytest tests/core/test_config.py -k docs_dir -v` → PASS.

- [ ] **Step 5: Write the failing context test.** In `tests/cli/test_context.py` (mirror the existing `mship context` invocation there — a `CliRunner` invoking `["context"]` and `json.loads(result.stdout)`), add:

```python
def test_context_includes_docs_dir(configured_workspace):
    result = runner.invoke(app, ["context"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["docs_dir"] == "docs"
```

(Use whatever configured-workspace fixture the other context tests use; if the test file has a different fixture name, match it.)

- [ ] **Step 6: Run → fail.** `uv run pytest tests/cli/test_context.py -k docs_dir -v` → FAIL (`KeyError: 'docs_dir'`).

- [ ] **Step 7: Surface it.** In `src/mship/cli/context.py`, right after the `payload = build_context(...)` call (~line 43, before the `resolve_task` block), add:

```python
        payload["docs_dir"] = container.config().docs_dir
```

- [ ] **Step 8: Run → pass.** `uv run pytest tests/cli/test_context.py -k docs_dir -v` → PASS. Then the whole two files: `uv run pytest tests/core/test_config.py tests/cli/test_context.py -q`.

- [ ] **Step 9: Commit.**

```bash
git add src/mship/core/config.py src/mship/cli/context.py tests/core/test_config.py tests/cli/test_context.py
git commit -m "feat(config): docs_dir (default docs); surface in mship context"
mship journal "added docs_dir config + mship context surfacing; tests pass" --action committed
```

---

## Task 2: Canonical "Specs" section in `working-with-mothership` (+ Tier 3 for that skill)

**Files:**
- Modify: `src/mship/skills/working-with-mothership/SKILL.md`

This is the single source of truth (Approach A). All other skills point here.

- [ ] **Step 1: Add the "Specs" section.** Insert a new section in the `plan`-phase area of `working-with-mothership/SKILL.md` (just after the phase-workflow description, before the "Delegating to subagents" section). Content:

````markdown
## Specs: the spec lifecycle (`mship spec`)

In a mothership workspace the **canonical design artifact is a structured `mship spec`**, not an ad-hoc design doc. A spec is the shared communication substrate: a durable, queryable artifact that agents hand off to each other and that humans review/steer from the mobile app over `mship serve`. The `plan` phase is where a spec is authored and approved.

A spec lives at `<workspace>/specs/<date>-<id>.md` — frontmatter (id, title, status, acceptance criteria, open questions, non-goals, risks, bound task) + a body with `Problem` / `User story` / `Approach` sections. Status flows:
`captured → drafting → needs_review → needs_clarification → approved → dispatched → implemented → archived`.

The lifecycle, in order:

```bash
mship spec new --title "<title>"            # create a stub (status: captured)
mship spec draft <id>                        # emit a drafting prompt to stdout
#   → run that prompt through an agent to produce SpecDraft JSON, then:
mship spec apply <id> --from-json <file>     # ingest the draft (→ needs_review)
mship spec validate <id>                     # check body structure
mship spec review <id>                       # print the review payload (criteria, questions, context)
mship spec verdict <id> <criterion-id> approved|flagged
mship spec questions <id>                    # list open questions
mship spec ask <id> "<question>"             # add a question
mship spec answer <id> <question-id> "<answer>"
mship spec approve <id> [--bypass-gate]      # → approved (gate: all criteria approved + questions answered)
mship spec request-changes <id> --reason "<why>"   # → needs_clarification
mship spec dispatch <id>                     # bind the approved spec to a task + emit a handoff
```

Review a spec without the CLI via `mship view spec [--web]`, or over HTTP via `mship serve` (the Ground Control phone path).

**The approval gate.** With `require_approved_spec: true` in `mothership.yaml`, `mship phase dev` is **hard-blocked** until a bound, approved spec exists — escape with `mship phase dev --bypass-spec-gate`. **This is opt-in: the default is OFF**, so by default `phase dev` only warns when no spec is found. Spec-first is the recommended methodology regardless of the gate.
````

- [ ] **Step 2: Fix the stale "soft gates" claim.** Find the soft-gates description (it currently says `phase dev` only ever "warns if no spec is found" / "soft gates warn, don't block"). Edit it to note the spec gate is a **hard block when `require_approved_spec: true`** (default OFF), cross-referencing the Specs section. Leave the tests/uncommitted gates described as soft warnings.

- [ ] **Step 3: Add Tier 3 coverage (this skill).** Make these additions:
  - In the inspection/integration area, add `mship serve` — "read + review/approve write API over the spec + task model (`--host`/`--port 47100`, bearer `MSHIP_SERVE_TOKEN` for non-loopback). The Ground Control phone surface."
  - In the journal/structured-logging area, add the debug thread commands and cross-ref: "`mship debug hypothesis|rule-out|resolved` record structured debugging entries (`mship test` auto-attaches to the open hypothesis). See the `systematic-debugging` skill."
  - In the `mship finish` synopsis, add the missing flags: `--require-tests` (block, don't warn, when test evidence is missing), `--title` (override PR title), `--body-map` (per-repo bodies), and note `--force`/`-f` re-pushes new commits to an already-finished task's existing PR.

- [ ] **Step 4: Verify structure + commit.** Confirm the file still has valid frontmatter and renders (eyeball headings). Then:

```bash
git add src/mship/skills/working-with-mothership/SKILL.md
git commit -m "docs(skills): canonical mship spec lifecycle section + serve/debug/finish coverage"
mship journal "working-with-mothership: spec lifecycle section + Tier 3 coverage" --action committed
```

---

## Task 3: `brainstorming` — dual-path output (→ `mship spec` in a workspace)

**Files:**
- Modify: `src/mship/skills/brainstorming/SKILL.md`

- [ ] **Step 1: Replace the design-doc output step.** The skill's checklist item 6 currently says "Write design doc — save to `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` and commit". Replace it (and the matching prose in the "After the Design / Documentation" section, ~line 29 and ~line 111) with a dual-path:

````markdown
6. **Capture the design** — dual-path on workspace detection (is there a `mothership.yaml` at/above cwd?):
   - **In a mothership workspace:** the design becomes a structured **`mship spec`**, not a doc file. Run `mship spec new --title "<title>"`, then populate it (`mship spec draft <id>` → agent → `mship spec apply <id> --from-json <file>`), landing it in `needs_review`. This is the canonical artifact — it gets reviewed/approved (`mship spec review`/`verdict`/`approve`) and dispatched. See the `working-with-mothership` skill for the full lifecycle.
   - **Outside a workspace:** write a plain design doc to `docs/specs/YYYY-MM-DD-<topic>-design.md` and commit it.
````

- [ ] **Step 2: Update the checklist + flow diagram terminal.** In the checklist and the `dot` process-flow graph, the terminal "Write design doc → … → Invoke writing-plans" path must reflect: in a workspace, the artifact is an `mship spec` that goes through review/approve before `writing-plans`. Update the "Write design doc" node label to "Capture design (mship spec in a workspace, else design doc)" and keep `writing-plans` as the terminal.

- [ ] **Step 3: Update the User Review Gate wording.** The gate currently says "Spec written and committed to `<path>`…". Make it path-aware: in a workspace, "the spec is in `needs_review` (`mship spec review <id>`)"; otherwise the design-doc path.

- [ ] **Step 4: Verify + commit.**

```bash
git add src/mship/skills/brainstorming/SKILL.md
git commit -m "docs(skills): brainstorming output converges on mship spec (dual-path)"
mship journal "brainstorming: dual-path output → mship spec" --action committed
```

---

## Task 4: `writing-plans`, `executing-plans`, `subagent-driven-development` cross-refs

**Files:**
- Modify: `src/mship/skills/writing-plans/SKILL.md`
- Modify: `src/mship/skills/executing-plans/SKILL.md`
- Modify: `src/mship/skills/subagent-driven-development/SKILL.md`

- [ ] **Step 1: `writing-plans`.** Add a short "Mothership workspace" note near the top: "In a mothership workspace, the input to a plan is an **approved `mship spec`** (reference its id in the plan header); `mship phase dev` may be gated on that approval (see `working-with-mothership`)." Change the "Save plans to" default from `docs/superpowers/plans/…` to **`<docs_dir>/plans/YYYY-MM-DD-<feature-name>.md`** (default `docs/plans/`; `docs_dir` comes from `mship context`). Outside a workspace, `docs/plans/`.

- [ ] **Step 2: `executing-plans`.** In the mothership-workspace pre-flight check (where it verifies `mship status` shows an active task), add: "If `require_approved_spec: true`, the task needs a bound, approved spec before `mship phase dev` will advance — create/approve one (`mship spec …`) or pass `mship phase dev --bypass-spec-gate`. See `working-with-mothership`."

- [ ] **Step 3: `subagent-driven-development`.** Near the existing `mship dispatch` guidance (the line Tier 1 corrected to include `-i`), add: "For a spec-driven kickoff, `mship spec dispatch <id>` binds an approved spec to its task and emits a handoff that includes the acceptance criteria — complementary to `mship dispatch -i`."

- [ ] **Step 4: Verify + commit.**

```bash
git add src/mship/skills/writing-plans/SKILL.md src/mship/skills/executing-plans/SKILL.md src/mship/skills/subagent-driven-development/SKILL.md
git commit -m "docs(skills): spec-aware cross-refs in writing-plans/executing-plans/subagent-driven-development"
mship journal "writing/executing/subagent skills: spec cross-refs" --action committed
```

---

## Task 5: `using-mothership` + `writing-skills` — `mship skill` coverage

**Files:**
- Modify: `src/mship/skills/using-mothership/SKILL.md`
- Modify: `src/mship/skills/writing-skills/SKILL.md`

- [ ] **Step 1: `using-mothership`.** After the "How to Access Skills" section, add a short "Installing bundled skills" note: "mship ships its skills under `src/mship/skills/`. `mship skill list` shows what's bundled; `mship skill install [--only claude,codex,gemini] [--force]` deploys them into the agent's skill directories."

- [ ] **Step 2: `writing-skills`.** Where it describes skill homes (`~/.claude/skills`, `~/.agents/skills/`), add: "In mothership, bundled skills live in `src/mship/skills/<name>/SKILL.md` and are distributed with `mship skill install`." In the deployment checklist (~line 631), add a step: "In the mothership repo, run `mship skill install` after committing to propagate the skill into agent dirs."

- [ ] **Step 3: Fix the self-contradicting link.** In `writing-skills/SKILL.md` (~line 556), the REFACTOR section uses `@testing-skills-with-subagents.md` — the `@` force-load syntax the skill itself prohibits (~line 286). Change it to a plain reference: `testing-skills-with-subagents.md`.

- [ ] **Step 4: Verify + commit.**

```bash
git add src/mship/skills/using-mothership/SKILL.md src/mship/skills/writing-skills/SKILL.md
git commit -m "docs(skills): mship skill list/install coverage; fix @-force-load link"
mship journal "using-mothership + writing-skills: mship skill coverage" --action committed
```

---

## Task 6: `test-driven-development`, `verification-before-completion`, `dispatching-parallel-agents`

**Files:**
- Modify: `src/mship/skills/test-driven-development/SKILL.md`
- Modify: `src/mship/skills/verification-before-completion/SKILL.md`
- Modify: `src/mship/skills/dispatching-parallel-agents/SKILL.md`

- [ ] **Step 1: `test-driven-development`.** In the verification/debugging-integration area, add one sentence: "In a mothership workspace, run tests via `mship test` (not a bare runner) — it records the evidence that `mship finish --require-tests` checks before opening a PR."

- [ ] **Step 2: `verification-before-completion`.** Add a short "mship workspace" callout: "In a mothership workspace, the verification step should use `mship test` so the result is recorded as evidence; `mship finish --require-tests` enforces that gate. See `working-with-mothership`."

- [ ] **Step 3: `dispatching-parallel-agents`.** Add a "Mothership workspace" callout mirroring the one in `subagent-driven-development`: "Before dispatching parallel agents in a workspace, confirm an anchored task (`mship status`) and set each agent's working dir to its task worktree (`.resolved_task.worktrees.<repo>`) — agents that run on `main` will be blocked by the pre-commit hook."

- [ ] **Step 4: Verify + commit.**

```bash
git add src/mship/skills/test-driven-development/SKILL.md src/mship/skills/verification-before-completion/SKILL.md src/mship/skills/dispatching-parallel-agents/SKILL.md
git commit -m "docs(skills): mship test evidence pipeline + parallel-dispatch worktree awareness"
mship journal "TDD/verification/parallel skills: evidence + worktree awareness" --action committed
```

---

## Task 7: `using-git-worktrees` — drop the `~/.config/superpowers/` global fallback

**Files:**
- Modify: `src/mship/skills/using-git-worktrees/SKILL.md`

- [ ] **Step 1: Remove the global-superpowers option.** The skill offers `~/.config/superpowers/worktrees/<project-name>/` as a worktree location (the "Ask User" dialog option 2 ~line 50, the "For Global Directory" section ~line 75, and the case branch ~line 97-98). Remove the global option; default the generic (non-mship) fallback to **project-local `.worktrees/`** only. Update the decision table (~line 154) accordingly.

- [ ] **Step 2: Sweep for residual references.** `grep -n 'superpowers' src/mship/skills/using-git-worktrees/SKILL.md` → should be empty.

- [ ] **Step 3: Verify + commit.**

```bash
git add src/mship/skills/using-git-worktrees/SKILL.md
git commit -m "docs(skills): drop stale ~/.config/superpowers worktree fallback"
mship journal "using-git-worktrees: dropped superpowers global fallback" --action committed
```

---

## Task 8: Guard test + full suite

**Files:**
- Create: `tests/core/test_skills_cli_refresh.py`

A regression guard so the skills can't silently re-drift on the things this refresh fixed.

- [ ] **Step 1: Write the guard test.** Create `tests/core/test_skills_cli_refresh.py`:

```python
"""Guard tests: keep the bundled skills aligned with the CLI after the Tier 2/3 refresh."""
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[2] / "src" / "mship" / "skills"

# Skills edited in the refresh that must not carry the legacy upstream paths.
EDITED_SKILLS = [
    "working-with-mothership", "brainstorming", "writing-plans", "executing-plans",
    "subagent-driven-development", "using-mothership", "writing-skills",
    "test-driven-development", "verification-before-completion",
    "dispatching-parallel-agents", "using-git-worktrees",
]


@pytest.mark.parametrize("skill", EDITED_SKILLS)
def test_no_superpowers_config_path(skill):
    text = (SKILLS_DIR / skill / "SKILL.md").read_text()
    assert "~/.config/superpowers" not in text, f"{skill}: stale superpowers worktree path"


def test_working_with_mothership_names_real_spec_commands():
    """Every `mship spec <sub>` named in the canonical skill must be a real subcommand."""
    text = (SKILLS_DIR / "working-with-mothership" / "SKILL.md").read_text()
    real_subcommands = {
        "new", "draft", "apply", "validate", "review", "verdict",
        "questions", "ask", "answer", "approve", "request-changes", "dispatch",
    }
    import re
    named = set(re.findall(r"mship spec ([a-z-]+)", text))
    bogus = named - real_subcommands
    assert not bogus, f"working-with-mothership names non-existent spec subcommands: {bogus}"


def test_dispatch_examples_include_instruction():
    """`mship dispatch` example invocations must include the required -i/--instruction."""
    import re
    for skill in ("working-with-mothership", "subagent-driven-development"):
        text = (SKILLS_DIR / skill / "SKILL.md").read_text()
        # any fenced example line that *invokes* dispatch must carry -i or --instruction
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"^(\$ )?mship dispatch\b", stripped):
                assert "-i" in stripped or "--instruction" in stripped, \
                    f"{skill}: `mship dispatch` example missing -i: {stripped!r}"
```

- [ ] **Step 2: Run → pass.** `uv run pytest tests/core/test_skills_cli_refresh.py -v`. If a case fails, the corresponding skill edit (Tasks 2–7) is incomplete — fix the skill, not the test.

- [ ] **Step 3: Full suite.** `mship test` (records evidence) → green. The pre-existing skill tests (`tests/cli/test_skill.py`, `tests/core/test_skills_namespace.py`, `tests/core/test_skill_install.py`) must still pass.

- [ ] **Step 4: Commit.**

```bash
git add tests/core/test_skills_cli_refresh.py
git commit -m "test(skills): guard against CLI drift (spec commands, dispatch -i, superpowers paths)"
mship journal "added skills↔CLI drift guard test; full suite green" --action committed
```

---

## Self-Review

- **Spec coverage:** §1 → Task 2 (canonical Specs section). §2 → Tasks 3 (brainstorming) + 4 (writing/executing/subagent). §3 → Tasks 2 (serve/debug/finish), 5 (skill install), 6 (evidence + parallel). §4 → Task 1 (`docs_dir` + context) + Task 7 (worktree fallback). Guard → Task 8.
- **Placeholder scan:** code tasks (1, 8) have complete code; doc tasks specify exact files, anchors, and the actual text to insert/replace. The few "match the existing fixture" notes (config/context test harness) are deliberate — the construction boilerplate is best read from the neighbouring tests rather than guessed.
- **Type consistency:** `WorkspaceConfig.docs_dir` (str, default `"docs"`); `payload["docs_dir"]` in `cli/context.py`; the guard test's `real_subcommands` set matches the verified `mship spec` subcommand list.
- **Scope:** does NOT touch `spec_paths` / `SPEC_SUBDIR` (out of scope per design); does NOT migrate existing `docs/superpowers/*` files.

## Execution Handoff

Spawn the task AFTER #175 merges (shared skill files), then implement task-by-task. Tasks 3–7 are independent doc edits over distinct files and can be parallelized; Task 1 (code) is independent; Task 8 (guard) runs last, after the skill edits land.
