# `mship spec draft` + `spec apply` — design (MOS-146 / A2)

> Status: design approved 2026-06-14. Feeds `writing-plans`.
> Part of **Ground Control** (epic MOS-144). Builds directly on the A1 Spec
> substrate (MOS-145, merged in #166).

## Context

A1 landed the structured `Spec` (model, lifecycle/transitions, frontmatter IO,
`SpecStore`, `find_spec`, and a task-optional `mship spec new` that scaffolds a
spec with a template body). A2 adds **drafting**: turning rambled intent into the
spec's structured fields (problem / user story / approach prose + non_goals,
risks, affected_repos, acceptance_criteria, open_questions).

`mship spec new` already covers *create*, so A2 is `draft` + the ingest step.

## Decisions

0. **Source of truth — markdown-canonical (confirmed), structure enforced.**
   Specs stay portable, readable, git-diffable `.md` files (no A1 rework). mship
   *forces* the structured shape rather than trusting it: the **frontmatter** is
   already validated by the pydantic `Spec` (A1); this task adds a **simple body
   parser/validator** that enforces the canonical sections (`## Problem /
   ## User story / ## Approach`). So "structured" is a *validated contract over
   markdown*, not a separate store.
1. **Agent boundary — emit prompt + ingest result (mirrors `mship dispatch`).**
   `mship spec draft` *emits* a drafting prompt; the caller (GC app / Claude Code)
   runs its own model; the structured result is fed back via `mship spec apply`.
   **mship never calls a model or generates content** — same boundary as
   `dispatch`, thesis intact.
2. **Prose lands as separate fields, mship renders the body.** The apply JSON
   carries `problem` / `user_story` / `approach` as separate strings; mship renders
   them into the canonical `## Problem / ## User story / ## Approach` body. This
   guarantees a consistent body structure that downstream review (A3) can rely on.
3. **mship owns criterion/question ids.** The model supplies text only;
   `apply` assigns deterministic `ac1…` / `q1…` ids (so A3 review verdicts have
   stable handles) and default verdicts (`unreviewed`) / `answer: null`.
4. **`apply` advances status `drafting → needs_review`** via the A1
   `validate_transition`; refused from other states unless `--bypass-status-gate`.

## Data model

A new `SpecDraft` pydantic model in `core/spec.py` — the draftable subset the
model is asked to produce:

```python
class SpecDraft(BaseModel):
    problem: str
    user_story: str
    approach: str
    non_goals: list[str] = []
    risks: list[str] = []
    affected_repos: list[str] = []
    acceptance_criteria: list[str] = []   # text only; mship assigns ids + verdict
    open_questions: list[str] = []        # text only; mship assigns ids; answer=None
```

## Commands

### `mship spec draft <id> [--from-text "…"] [--from-file <path>]`
Emits a self-contained drafting prompt to **stdout** (agent-agnostic markdown,
built by a new `core/spec_draft.py` mirroring `core/dispatch.py`). The prompt
contains:
- the **intent text** — from `--from-text` (inline) or `--from-file` (any text,
  e.g. a freeform `docs/superpowers/specs` design doc). **Exactly one** of the two
  is required; error if neither or both are given. (No body-fallback — it would
  blur "real prose" vs. the `spec new` template placeholders.)
- the **`SpecDraft` JSON shape** + a short example, and an explicit instruction to
  **output ONLY valid JSON**;
- the apply command to pipe the result back: `mship spec apply <id> --from-json <file>`.

mship makes **no model call**.

### `mship spec apply <id> --from-json <path>` (or `-` for stdin)
Ingests the model's JSON:
- parse + validate against `SpecDraft` (invalid → clean `SpecParseError`-style error);
- render the body from `problem`/`user_story`/`approach`;
- set `non_goals`, `risks`, `affected_repos`;
- build `acceptance_criteria` = `[AcceptanceCriterion(id=f"ac{i+1}", text=t) …]`
  and `open_questions` = `[OpenQuestion(id=f"q{i+1}", text=t) …]`;
- preserve `id`/`created_at`/`task_slug`; bump `updated_at`;
- `validate_transition(spec.status, "needs_review")` (allowed from `drafting` and
  `needs_clarification`); refuse otherwise unless `--bypass-status-gate`;
- atomic write via `SpecStore`.

### Body structure: render + validate (the "simple parser")
Canonical sections are `## Problem`, `## User story`, `## Approach`. Add small,
pure helpers (in `core/spec_store.py` or a focused `core/spec_body.py`):
- `render_body(problem, user_story, approach) -> str` — produce the canonical
  body (used by `apply`, so its output always conforms).
- `parse_body_sections(body) -> dict[str, str]` — split a body by `## ` headings.
- `validate_body_structure(body) -> list[str]` — names of any missing required
  sections (empty list = conformant).

Non-strict on load (a hand-edited / legacy body still parses); they power the
explicit `spec validate` command and guarantee `apply`'s rendered output.

### `mship spec validate <id>`
Reports whether a spec conforms to the structured contract: frontmatter parses +
validates against `Spec` (via `parse_spec`), and `validate_body_structure` finds
no missing sections. Exit non-zero + list problems otherwise. This is the
"validate / force a structured markdown file" surface — keeps the file portable
while letting mship enforce the shape.

## Scope (A2 only)

`SpecDraft` model + the body render/validate helpers + the `spec_draft` prompt
builder + three CLI commands (`draft`, `apply`, `validate`) + the apply/merge
helper. **Out of scope:** `spec review` (A3), `spec questions`
(A4), `spec approve` (A5), `dispatch --spec` (A6), the serve API (B1). `apply`
only advances to `needs_review`.

## Migration / compat

Purely additive — no change to existing commands or stored specs. A spec drafted
by hand (edited body, no `apply`) still works; `apply` is the assisted path.

## Testing

- **draft:** emits a deterministic prompt containing the intent, the `SpecDraft`
  shape, and the apply instruction — asserted on output bytes, no model call.
  Input-source resolution: `--from-text` xor `--from-file` (error when neither or
  both).
- **apply:** valid JSON → body rendered from prose, structured fields set, `ac/q`
  ids assigned deterministically, status → `needs_review`, round-trips through
  `SpecStore`. Invalid JSON → error. Wrong-status spec (e.g. `approved`) →
  refused without `--bypass-status-gate`.
- **body helpers / validate:** `render_body` emits all three canonical sections;
  `parse_body_sections` round-trips a rendered body; `validate_body_structure`
  flags missing sections. `mship spec validate` passes on an apply-produced spec
  and fails (non-zero, naming the gap) on a spec missing a section or with invalid
  frontmatter.

## Follow-ons

- A3 (`spec review`) consumes the `acceptance_criteria` ids this assigns.
- B1 (`mship serve`) will wrap `draft`/`apply` as API endpoints; the GC app's
  Capture screen (C3) calls them.
