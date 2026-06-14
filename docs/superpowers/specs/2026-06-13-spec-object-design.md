# Structured Spec object — design (MOS-145)

> Status: design approved 2026-06-13. Feeds `writing-plans`.
> Part of the **Ground Control** initiative (Linear epic MOS-144). This is the
> keystone every other workstream-A/B/C issue depends on.

## Context

Ground Control's loop is `capture → draft → review → approve → dispatch`. That
requires **Specs as first-class objects** that exist *before* any task and carry
structured fields (status, acceptance criteria, open questions) so a mobile
review surface and a serve API can drive them.

Today mship has none of that:

- `mship spec new` scaffolds a **freeform** markdown stub at the task-scoped
  blessed path `.mothership/tasks/<slug>/SPEC.md` (`src/mship/cli/spec.py`).
- Specs are resolved by `find_spec` (`src/mship/core/view/spec_discovery.py`)
  with a newest-by-mtime fallback (the MOS-140 bug).
- `WorkspaceState` (`src/mship/core/state.py`) holds structured `Task` objects in
  `.mothership/state.yaml` (atomic write + flock); there is **no** Spec model and
  specs are **task-scoped** — you need a task first.

This issue (A1) builds the structured Spec substrate. The verbs that consume it —
`spec draft` (A2 / MOS-146), `spec review` (A3 / MOS-147), `spec questions`
(A4 / MOS-148), `spec approve` (A5 / MOS-149), `dispatch --spec` (A6 / MOS-150) —
are separate issues.

## Decisions (and rationale)

1. **Source of truth: markdown-canonical.** A Spec is a markdown file with YAML
   frontmatter; mship parses (and writes) it. Keeps specs git-diffable,
   PR-reviewable, and human-authored — consistent with the existing
   `docs/superpowers/specs` convention. mship state never duplicates spec
   content (no divergence).

2. **Location: a dedicated `specs/` dir at the workspace root.** Live,
   lifecycle-tracked Specs live in `<workspace_root>/specs/`, distinct from the
   freeform design docs in `docs/superpowers/specs` (which remain brainstorming
   *inputs* a `spec draft` can consume). The registry is a scan of `specs/`.

3. **Branch convention (resolves MOS-141): specs are workspace-level.** Because
   `specs/` lives at the workspace-repo root, and feature branches are cut in the
   **member** repos (not the workspace repo), the "which branch is the spec on"
   ambiguity dissolves — specs sit on the workspace repo's default branch,
   independent of member-repo branches. Specs are captured before any task/branch
   exists; dispatch cuts the member-repo branch *from* current state.

4. **Lifecycle: Spec.status and Task.phase are independent, synced at two
   boundaries.** Spec.status owns pre-task shaping; `Task.phase`
   (plan/dev/review/run) owns execution. They touch only at **dispatch**
   (`approved → dispatched`, create task) and **finish** (`dispatched →
   implemented`). Post-dispatch, Spec.status is a coarse overlay, not a mirror of
   phase.

## Data model

A `Spec` is parsed from frontmatter (pydantic model, mirroring the `Task`
pattern). Body prose is preserved verbatim and round-tripped.

```yaml
---
id: ground-control-decision-queue     # stable kebab id; also the filename slug
title: Ground Control decision queue
status: needs_review
created_at: 2026-06-13T10:00:00Z
updated_at: 2026-06-13T11:30:00Z
affected_repos: [mothership, ground-control]
acceptance_criteria:
  - { id: ac1, text: "User can view blocking questions", verdict: unreviewed }
  - { id: ac2, text: "Answer is recorded in mship journal", verdict: approved }
open_questions:
  - { id: q1, text: "Android in v0?", answer: null }   # answer: null = open
non_goals: ["real-time chat", "mobile IDE"]
risks: ["voice in v0 expands scope"]
task_slug: null                        # set at dispatch (task pointer)
---

## Problem
<prose>
## User story
<prose>
## Approach
<prose>
```

Field notes:

- `acceptance_criteria[].verdict ∈ {unreviewed, approved, flagged}` — addressable
  review units for A3.
- `open_questions[].answer = null` means open — gates approval (A5).
- `dispatch_ready` is **derived** (`status == approved` ∧ no open questions), not
  stored, so it can't drift.
- Filename `specs/YYYY-MM-DD-<id>.md`; `id` is also in frontmatter so discovery
  never has to guess by mtime.

## Status lifecycle

```
captured → drafting → needs_review ⇄ needs_clarification
                          │
                          ▼
                       approved ──(dispatch)──► dispatched ──► implemented ──► archived
                          ▲                                                       ▲
                          └──────(re-open / request-changes)                      │
   any non-terminal ──────────────────(abandon)─────────────────────────────────┘
```

A central transition map is the single authority; illegal transitions raise.
Allowed edges:

| From | To |
|------|----|
| captured | drafting |
| drafting | needs_review |
| needs_review | needs_clarification, approved |
| needs_clarification | needs_review, drafting |
| approved | dispatched, needs_clarification *(re-open)* |
| dispatched | implemented |
| implemented | archived |
| *any non-terminal* | archived *(abandon)* |

`approved → dispatched` is performed **only** by `mship dispatch --spec` (A6);
`dispatched → implemented` only by `mship finish`. No other path crosses the
spec/task boundary.

## Relationship to Task

- `Task` gains `spec_id: str | None` (default `None`); the spec frontmatter gains
  `task_slug: str | None`. Both set at dispatch — a **bidirectional pointer, not
  duplicated content**.
- Backward-compatible: old `state.yaml` loads cleanly (pydantic default +
  existing `extra="ignore"` on `WorkspaceState`).

## Discovery / registry

- Registry = scan `<workspace_root>/specs/*.md`, parse frontmatter, key by `id`.
  Exact-`id` match is authoritative — **subsumes the MOS-140 newest-by-mtime bug**
  for the new dir.
- `mship view spec` is extended to resolve from `specs/` by id. Legacy paths
  (`docs/superpowers/specs`, `.mothership/tasks/<slug>/SPEC.md`) stay supported.

## Scope of this issue (A1)

A1 ships the **substrate only**:

- `Spec` pydantic model.
- `parse_spec(path) → Spec` and `write_spec(Spec) → path` with faithful
  frontmatter round-trip (body preserved verbatim).
- Registry scan + id lookup.
- Transition validator (the table above).
- `Task.spec_id` field + back-compat load.
- `mship spec new` migrated to emit the new frontmatter format into `specs/`
  (keeps the command; changes template + location). **Now task-optional:** with
  no resolvable task it creates a standalone spec (`status: drafting`); when a
  task *is* resolvable it prefills `affected_repos` + binds `task_slug`. (Rich
  creation/`draft` is A2 — A1 only needs a working create path to exercise the
  model.)
- Atomic file writes (tmp + replace, mirroring `StateManager`); the `task↔spec`
  pointer in `state.yaml` uses the existing locked `mutate`.

**Out of scope (separate issues):** `spec draft` agent boundary (A2), review-unit
emission (A3), questions (A4), approve/request-changes (A5), dispatch wiring (A6),
the serve API (B1).

## Migration & back-compat

- Legacy freeform specs remain *readable* by `mship view spec`; **no forced
  migration**. An optional `mship spec import` can follow later.
- The 54 historical design docs in `docs/superpowers/specs` stay as freeform
  brainstorming inputs.

## Testing

- **Unit:** frontmatter round-trip is identity (parse→write→parse, body
  preserved); transition validator accepts every legal edge and rejects every
  illegal one; registry scan + id match; `Task.spec_id` back-compat load of old
  `state.yaml`.
- **Integration:** `mship spec new` produces a valid frontmatter'd file in
  `specs/`; a status transition updates frontmatter atomically.

## Follow-ons

- MOS-141 is effectively resolved by decision (3); close it with a note pointing
  here, or fold its docs change into this work.
- MOS-140's fix is subsumed for `specs/`; the legacy `docs/superpowers/specs`
  newest-by-mtime path can still get the filename-match fix independently.
