# Complete the spec lifecycle — design (A4–A7: MOS-148/149/150/151)

> Status: design 2026-06-14. Autonomous combined effort (one PR). Design calls made
> by the agent and documented here for PR review (no interactive brainstorm — the
> reviewer evaluates the whole PR). Builds on A1–A3 (Spec model, draft/apply,
> review/verdict — all merged).
> **⚠️ Judgment calls flagged inline with 🟡 for reviewer attention.**

Completes the `mship spec` lifecycle: `new → draft → apply → review/verdict →
**questions → approve → dispatch** → (gate)`.

---

## A4 — `spec questions` (MOS-148)

Manage the open questions on a spec (the model already has
`open_questions: [{id, text, answer}]`).

- **`mship spec questions <id>`** — list open questions (non-TTY JSON / TTY readable).
- **`mship spec ask <id> "<text>"`** — append a question; assign the next `q<n>` id
  (max existing + 1); save; bump `updated_at`.
- **`mship spec answer <id> <q-id> "<answer>"`** — set a question's `answer`;
  unknown `q-id` errors (lists valid ids); save.
- 🟡 **Answering does not auto-transition status** (a question is an annotation;
  A5's `approve`/`request-changes` own status). Keeps A4 a pure CRUD layer.
- Core helpers in `core/spec_questions.py`: `add_question(spec, text)`,
  `answer_question(spec, q_id, answer)`, `list_questions(spec) -> list[dict]`.

## A5 — `spec approve` / `request-changes` (MOS-149)

- **`mship spec approve <id> [--bypass-gate]`** — the approval gate. Refuses unless
  **every acceptance criterion verdict == `approved`** AND **every open question is
  answered** (`answer is not None`); on failure, prints exactly what's blocking and
  exits non-zero. With the gate satisfied (or `--bypass-gate`),
  `validate_transition(status, "approved")` → set `approved`, save. (Legal only from
  `needs_review` per the A1 map.)
- **`mship spec request-changes <id> --reason "<why>"`** —
  `validate_transition(status, "needs_clarification")` → set `needs_clarification`,
  save. 🟡 **The `--reason` is echoed + journaled (`mship journal`), not persisted on
  the spec** — avoids a model field this version; a `change_requests[]` field is a
  clean follow-on if reviewers want the reason on the artifact.
- Gate logic in `core/spec_approve.py`: `approval_blockers(spec) -> list[str]`
  (empty == approvable), reused by the CLI.

## A6 — dispatch an approved spec (MOS-150) 🟡 **(integration-risk — review closely)**

🟡 **Command shape:** `mship spec dispatch <id>` (a new subcommand under `spec`),
**not** an overload of the existing `mship dispatch`. Rationale: `mship dispatch`
today emits a prompt for an *existing* task (`--instruction` required); making it
*also create* a task from a spec would overload its contract and risk regressions.
A dedicated `spec dispatch` keeps that boundary clean. (The MOS-150 title says
`dispatch --spec`; this is the same intent, cleaner surface — flagged for the
reviewer to confirm.)

Behavior:
1. Require `spec.status == approved` (else error: "approve it first"). 🟡 ungated by
   `--bypass` here — dispatch from an unapproved spec defeats the lifecycle; can add
   a bypass later if needed.
2. **Reuse the existing spawn machinery** to create a task for `spec.affected_repos`,
   slug derived from `spec.id` (worktrees + branch, exactly like `mship spawn`).
3. Bind both directions: `task.spec_id = spec.id`; `spec.status = dispatched`
   (`validate_transition`), `spec.task_slug = task.slug`; save both.
4. Emit the standard handoff prompt (`build_dispatch_prompt`) seeded from the spec —
   instruction = the spec's problem + acceptance criteria.

🟡 **Risk:** this touches task-creation + worktree side effects, so its tests need a
git-backed workspace fixture (conftest `workspace_with_git` / `audit_workspace`).
If the spawn integration proves too entangled to do cleanly in one task, the
fallback is a **lighter A6**: bind the spec to an *already-spawned* task + emit the
prompt (no auto-spawn), and the auto-spawn becomes a follow-on. This fallback will
be called out in the PR if taken.

## A7 — harden the `plan → dev` gate (MOS-151)

- `mship phase` `plan → dev` **optionally** requires a **bound, approved** spec (a spec with
  `task_slug == <task>` and `status` in {`approved`, `dispatched`, `implemented`}).
  The gate is **config-gated and off by default** via `require_approved_spec: true` in
  `mothership.yaml`. This avoids breaking the existing `spawn → phase dev` workflow
  for workspaces that have not yet adopted the spec lifecycle. Workspaces that opt in
  get the hard block; all others keep the existing soft warning.
- **`--bypass-spec-gate`** overrides the hard block for one-off exceptions
  (per the `--bypass-…` naming convention).
- Implemented additively in `core/phase.py`: keep the existing spec-existence warning
  (still fires as a soft gate when `require_approved_spec` is False), and add the
  hard-block on top when the flag is True. The `SpecGateError` is raised before
  any state mutation, so partial transitions cannot occur.

**Why default-off?** A hard-on default would block every existing `plan→dev`
transition that lacks an approved spec, breaking all current workspaces and tests.
The blast radius is too large for a v1 gate. Workspaces can opt in when they have
integrated the spec workflow end-to-end.

---

## Scope / out of scope

In: the four commands + their core helpers + the gate change. Out: per-prose-card
verdicts (A3 follow-on), the serve API (B1), the GC app (C), persisting the
request-changes reason (flagged follow-on).

## Migration / compat

A4/A5/A6 are additive (new commands). A7 adds a config-gated gate to `phase plan→dev` —
default-off means zero impact on existing workspaces. Workspaces that set
`require_approved_spec: true` get the hard block; `--bypass-spec-gate` provides an
escape hatch. The gate is covered by new tests.

## Testing

- A4: ask assigns sequential `q` ids; answer sets/【unknown-id errors】; list/JSON shape.
- A5: `approval_blockers` (flagged criterion → blocked; unanswered question → blocked;
  all-clear → empty); `approve` refuses when blocked, succeeds when clear / bypassed,
  rejects from non-`needs_review` status; `request-changes` → `needs_clarification`.
- A6: dispatch from approved spec creates a task (git fixture), binds `task.spec_id`
  + `spec.status == dispatched`, emits a prompt containing the acceptance criteria;
  refuses an unapproved spec.
- A7: `plan→dev` refused without an approved bound spec; allowed with one; allowed
  with `--bypass-spec-gate`; existing gate tests adjusted.
