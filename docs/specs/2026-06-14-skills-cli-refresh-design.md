# Skills ↔ CLI refresh — design (Tier 2/3)

> Status: design approved 2026-06-14. Feeds `writing-plans`.
> Follows the skills-vs-CLI audit. Tier 1 (literal command/path errors) shipped
> separately in [#175](https://github.com/atomikpanda/mothership/pull/175) (MOS-167).

## Context

The mship-bundled skills (`src/mship/skills/`) are where mothership's methodology
lives — they're a fork of superpowers, shipped + installed via `mship skill`. An
audit of all 15 skills against the current `mship` CLI found they **predate the
`mship spec` lifecycle (A1–A7)** and several other commands: no skill teaches the
spec-driven workflow, and a few carry upstream-superpowers artifacts (paths, a
self-contradicting force-load link).

Tier 1 (unambiguous literal errors — `dispatch -i`, `view journal`, worktree path)
already shipped. **This design covers Tier 2 (weave the spec lifecycle into the
skills) + Tier 3 (the remaining coverage gaps), as one coherent refresh.**

## Decisions

1. **Converge on `mship spec`.** In a mothership workspace the canonical design
   artifact is the structured `mship spec` (`<workspace>/specs/<date>-<id>.md`),
   not a free-form design doc. Brainstorming's output flows into the spec lifecycle;
   the structured spec is the reviewable/approvable/dispatchable contract that
   Ground Control reads from the phone.
   - **Why (the motivating principle):** the structured spec is the *shared
     communication substrate*. A durable, queryable artifact that agents hand off to
     each other and that humans review and steer from the mobile app over
     `mship serve`. It replaces ad-hoc design docs and state held only in an agent's
     working memory — both of which are invisible to other agents and to the phone.
     The skills must stop producing ad-hoc docs in a workspace and instead write into
     this shared state.
2. **Dual-path with detection.** Skills detect the workspace (`mothership.yaml` at/
   above cwd → mship path; else a clean generic fallback). This keeps the general
   methodology skills usable in single-repo / non-mship contexts.
3. **Approach A — centralized lifecycle.** `working-with-mothership` owns the single
   canonical description of the spec lifecycle; every other skill gets a *thin*
   mship-aware cross-reference, not a copy. This structurally prevents the
   duplication-drift the audit just cleaned up.
4. **Configurable docs location.** New `docs_dir` config key (default `docs`)
   replaces the upstream `docs/superpowers/` convention with a sensible, overridable
   default. Surfaced to agents via `mship context` so skills don't parse YAML.

## §1 — Canonical "Specs" section (in `working-with-mothership`)

A new section, integrated into the `plan` phase, is the single source of truth:

- **What a spec is** — the structured artifact at `<workspace>/specs/<date>-<id>.md`
  (frontmatter + `Problem`/`User story`/`Approach` body, acceptance criteria, open
  questions, non-goals, risks) and its status lifecycle
  (`captured → drafting → needs_review → needs_clarification → approved → dispatched
  → implemented → archived`).
- **The command loop, in order:** `mship spec new` → `mship spec draft` (emits a
  drafting prompt) → run it → `mship spec apply` (ingest the `SpecDraft`,
  → `needs_review`) → `mship spec validate` → `mship spec review` /
  `mship spec verdict <criterion-id> approved|flagged` → `mship spec questions` /
  `ask` / `answer` → `mship spec approve [--bypass-gate]` /
  `mship spec request-changes --reason` → `mship spec dispatch` (binds the approved
  spec to a task + emits the handoff).
- **The gate** — `require_approved_spec: true` in `mothership.yaml` makes `plan→dev`
  require an approved spec; `mship phase --bypass-spec-gate` escapes it. **Default
  OFF.** The section presents spec-first as the *recommended methodology* while being
  explicit the hard gate is opt-in. (This corrects the skill's current "soft gates
  always warn, never block" claim, which is false when the gate is enabled.)
- **Reviewing specs** — pointer to `mship view spec [--web]` and `mship serve` (the
  Ground Control / phone review path).

## §2 — Thin cross-references in the other 4 workflow skills

Each gets a short mship-aware branch pointing to §1, never a copy of the lifecycle:

- **`brainstorming`** — its output step changes. *In a workspace*: the design becomes
  an `mship spec` (`spec new` → populate → `needs_review`), then review/approve, then
  → `writing-plans`. Its checklist + process-flow diagram terminal update to match.
  *Outside a workspace*: a plain design doc at `docs/specs/YYYY-MM-DD-<topic>-design.md`.
- **`writing-plans`** — in a workspace the plan takes an **approved spec** as input
  (references its id) and notes the gate; plan docs go to `<docs_dir>/plans/`.
- **`executing-plans`** — adds the spec-gate caveat before spawning / advancing
  `plan→dev` (check for an approved spec or `--bypass-spec-gate`).
- **`subagent-driven-development`** — notes `mship spec dispatch` as the spec-bound
  kickoff (complements the `mship dispatch -i` Tier 1 already corrected).

## §3 — Tier 3 coverage fixes (folded in where files are already open)

- **`using-mothership`** — short "installing skills" note: `mship skill list` /
  `mship skill install [--only claude,codex,gemini] [--force]`.
- **`writing-skills`** — the `src/mship/skills/<name>/SKILL.md` bundled-skill location
  + `mship skill install` distribution step; fix the self-contradicting `@`-force-load
  link (currently ~line 556) to a plain reference.
- **`working-with-mothership`** — brief coverage of `mship serve` (read+write spec/task
  API), `mship debug hypothesis|rule-out|resolved` (cross-ref `systematic-debugging`),
  and the missing `mship finish` flags (`--require-tests`, `--title`, `--body-map`,
  `--force` re-push).
- **`test-driven-development`** + **`verification-before-completion`** — note that
  `mship test` is the evidence-recording runner whose results feed
  `mship finish --require-tests`.
- **`dispatching-parallel-agents`** — a workspace/worktree-awareness callout (each
  parallel agent works from its task worktree, not main).

## §4 — Dual-path detection + `docs_dir` config

- **Detection convention** (shared phrasing across skills): *"if `mothership.yaml`
  exists at/above cwd → mship path; else generic fallback."*
- **`docs_dir` config** — add `docs_dir: str = "docs"` to **`WorkspaceConfig`**
  (`core/config.py`, beside `spec_paths` / `require_approved_spec`); surface it in
  `mship context` so a skill can read it without parsing YAML, via a one-liner in
  `cli/context.py` (`payload["docs_dir"] = container.config().docs_dir`) — no change
  to the cached `build_context`.
  - `docs_dir` governs **plan-doc location only** → `<docs_dir>/plans/` (default
    `docs/plans/`). Plans have no CLI representation, so this is non-conflicting.
  - Non-mship fallback design docs → `docs/specs/` (hardcoded default; no config
    outside a workspace).
  - In-workspace specs stay canonical `mship spec` files at `<workspace>/specs/`
    (`SPECS_DIRNAME`) — **unaffected** by `docs_dir`.
  - **Not touched:** the existing `spec_paths` config and its `SPEC_SUBDIR`
    (`docs/superpowers/specs`) legacy free-form spec-search default. Spec discovery
    already searches the canonical `specs/` *and* `spec_paths`, so convergence makes
    that legacy default vestigial but harmless; changing it is separate behavioral
    work (see Out of scope).
- **`using-git-worktrees`** — drop the stale `~/.config/superpowers/worktrees/<project>/`
  global option; default the generic fallback to project-local `.worktrees/`.

## Architecture / units of work

1. **Code:** `docs_dir: str = "docs"` on `WorkspaceConfig` (`core/config.py`, beside
   `spec_paths`) + expose in `mship context` via the `cli/context.py` one-liner
   (no change to the cached `build_context`). Tests: config default + override;
   `mship context` payload includes `docs_dir`. Does NOT touch `spec_paths`/`SPEC_SUBDIR`.
2. **Canonical doc:** the new Specs section in `working-with-mothership/SKILL.md`
   (§1 + the §3 items that belong to that skill).
3. **Cross-reference edits:** `brainstorming`, `writing-plans`, `executing-plans`,
   `subagent-driven-development` (§2).
4. **Tier 3 edits:** `using-mothership`, `writing-skills`, `test-driven-development`,
   `verification-before-completion`, `dispatching-parallel-agents` (§3).
5. **`using-git-worktrees`:** §4 worktree-fallback cleanup.

These are mostly independent doc edits over distinct files (parallelizable), plus the
one small code unit (1) they depend on for the `docs_dir` reference.

## Migration / compat

- Additive config: `docs_dir` defaults to `docs`, so unset workspaces get the new
  default with no action.
- Existing `docs/superpowers/*` files are left as historical, dated artifacts — no
  migration. New docs use the `docs/` default.
- All skill changes are documentation; the only behavioral code change is the
  additive `docs_dir` field (no existing behavior changes when it's unset).

## Testing

- **Code:** `Config` parses `docs_dir` (default `docs`; override respected);
  `mship context` includes `docs_dir`.
- **Docs:** existing skill-namespace / install tests still pass; a sweep confirms no
  residual `docs/superpowers/` or `~/.config/superpowers/` references remain in the
  edited skills, and that the spec-lifecycle commands named in `working-with-mothership`
  all exist in the CLI (guard against re-drift).

## Out of scope

- Migrating existing `docs/superpowers/*` files.
- A `mship plan` CLI artifact (plans remain doc files).
- Retroactively converting this design doc into an `mship spec` (the brainstorming→
  spec wiring is what this designs; it doesn't exist yet).
- Any change to the spec lifecycle commands themselves — this refresh documents the
  CLI as it is, it doesn't extend it.
- Reconciling the legacy `spec_paths` / `SPEC_SUBDIR` default (`docs/superpowers/specs`)
  used by free-form spec discovery. Convergence on `mship spec` makes it vestigial,
  but changing its default alters soft-gate / `mship view spec` search behavior for
  unconfigured workspaces (with test impact) — a separate follow-up if the
  `superpowers` reference should be retired everywhere.
