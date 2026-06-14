# Complete the Spec Lifecycle — Implementation Plan (A4–A7)

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. One combined branch/PR. Order: A4 → A5 → A7 → A6 (A6 last — highest integration risk). Steps use checkbox (`- [ ]`).

**Goal:** Finish the `mship spec` CLI lifecycle: `questions` (A4), `approve`/`request-changes` (A5), harden the `plan→dev` gate (A7), and `spec dispatch` (A6).

**Architecture:** Thin core helpers per feature + Typer commands on the existing `spec` sub-app; reuse A1–A3 primitives (`Spec`, `validate_transition`, `SpecStore`, `parse_body_sections`). Design + 🟡 judgment calls: [docs/superpowers/specs/2026-06-14-spec-lifecycle-design.md](../specs/2026-06-14-spec-lifecycle-design.md).

**Tech Stack:** Python, pydantic v2, Typer, pytest. Builds on A1–A3 (merged).

---

## File structure
- **Create** `core/spec_questions.py` (A4), `core/spec_approve.py` (A5).
- **Modify** `cli/spec.py` (A4/A5/A6 commands), `core/phase.py` + `cli/phase.py` (A7 gate), `core/state.py` already has `Task.spec_id` (A1).
- **Test** `tests/core/test_spec_questions.py`, `tests/core/test_spec_approve.py`, `tests/cli/test_spec.py` (extend), `tests/core/test_phase.py` (extend), `tests/cli/test_spec_dispatch.py` (A6, git-fixture).

Per-task: `uv run pytest <path>`; full suite via `mship test` at the end.

---

## Task A4 — `spec questions` / `ask` / `answer`

**Files:** Create `src/mship/core/spec_questions.py`; modify `cli/spec.py`; test `tests/core/test_spec_questions.py` + `tests/cli/test_spec.py`.

- [ ] **Core helpers (TDD).** `src/mship/core/spec_questions.py`:

```python
from __future__ import annotations
from mship.core.spec import OpenQuestion, Spec


def add_question(spec: Spec, text: str) -> OpenQuestion:
    """Append a question with the next q<n> id (max existing + 1)."""
    nums = [int(q.id[1:]) for q in spec.open_questions if q.id.startswith("q") and q.id[1:].isdigit()]
    q = OpenQuestion(id=f"q{(max(nums) + 1) if nums else 1}", text=text)
    spec.open_questions.append(q)
    return q


def answer_question(spec: Spec, q_id: str, answer: str) -> Spec:
    for q in spec.open_questions:
        if q.id == q_id:
            q.answer = answer
            return spec
    valid = ", ".join(q.id for q in spec.open_questions) or "(none)"
    raise ValueError(f"no open question {q_id!r}; valid ids: {valid}")


def list_questions(spec: Spec) -> list[dict]:
    return [{"id": q.id, "text": q.text, "answer": q.answer} for q in spec.open_questions]
```

Tests (`tests/core/test_spec_questions.py`): `add_question` assigns `q1` then `q2` (and `q<max+1>` when a draft already seeded `q1`); `answer_question` sets the answer and raises `ValueError` on unknown id; `list_questions` shape.

- [ ] **CLI (TDD).** In `cli/spec.py` `register`, add three commands following the established pattern (`get_container` → `SpecStore` → load → mutate → `updated_at` → save; TTY vs `output.json`):
  - `spec questions <id>` → `output.json(list_questions(spec))` / TTY readable. Unknown spec id errors.
  - `spec ask <id> <text>` → `add_question`, save; emit the new q-id.
  - `spec answer <id> <q-id> <answer>` → `answer_question` (catch `ValueError` → `output.error` + Exit 1), save.

  Tests (`tests/cli/test_spec.py`): after `new`+`apply` (which seeds `q1`), `ask dq "..."` → `q2`; `answer dq q1 "yes"` then `review dq` shows the answer; unknown q-id errors. (Answering does NOT change status — assert status unchanged.)

- [ ] **Commit** (one per sub-step or combined): `feat(spec): spec questions (ask/answer/list)` + `mship journal`.

---

## Task A5 — `spec approve` / `request-changes`

**Files:** Create `src/mship/core/spec_approve.py`; modify `cli/spec.py`; test `tests/core/test_spec_approve.py` + `tests/cli/test_spec.py`.

- [ ] **Core helper (TDD).** `src/mship/core/spec_approve.py`:

```python
from __future__ import annotations
from mship.core.spec import Spec


def approval_blockers(spec: Spec) -> list[str]:
    """Reasons a spec can't be approved (empty list == approvable)."""
    blockers: list[str] = []
    bad = [c.id for c in spec.acceptance_criteria if c.verdict != "approved"]
    if bad:
        blockers.append(f"acceptance criteria not approved: {', '.join(bad)}")
    unanswered = [q.id for q in spec.open_questions if q.answer is None]
    if unanswered:
        blockers.append(f"open questions unanswered: {', '.join(unanswered)}")
    if not spec.acceptance_criteria:
        blockers.append("no acceptance criteria")
    return blockers
```

Tests (`tests/core/test_spec_approve.py`): flagged/unreviewed criterion → blocked; unanswered question → blocked; no criteria → blocked; all-approved + all-answered → `[]`.

- [ ] **CLI (TDD).** In `cli/spec.py`:
  - `spec approve <id> [--bypass-gate]` → if `not bypass_gate` and `approval_blockers(spec)` non-empty → print blockers, Exit 1. Else `validate_transition(spec.status, "approved")` (catch `InvalidTransition` → Exit 1 with the message), set `status="approved"`, `updated_at`, save.
  - `spec request-changes <id> --reason <reason>` → `validate_transition(spec.status, "needs_clarification")`, set status, `updated_at`, save; echo + `mship journal` the reason (🟡 not persisted on the spec — see design).

  Tests (`tests/cli/test_spec.py`): approve refused while a criterion is unreviewed (lists it); after `verdict dq ac1 approved` (+ answering q1) approve succeeds → status `approved`; `--bypass-gate` approves despite blockers; approve from a non-`needs_review` status errors; `request-changes dq --reason x` → status `needs_clarification`.

- [ ] **Commit:** `feat(spec): spec approve + request-changes with approval gate` + journal.

---

## Task A7 — harden `plan → dev` gate

**Files:** Modify `src/mship/core/phase.py` (+ `src/mship/cli/phase.py` to thread the bypass flag); test `tests/core/test_phase.py` + the existing `tests/cli/test_spec.py` gate tests.

- [ ] **Read first:** `core/phase.py` `transition` + `_check_gates` + `_gate_dev` (currently warning-only), and `cli/phase.py` (how `transition` is invoked + how warnings are surfaced).
- [ ] **TDD.** Add a HARD requirement for `plan→dev`: there must be a bound, approved spec (a spec with `task_slug == task_slug` and `status` in `{approved, dispatched, implemented}`), unless bypassed.
  - Add `bypass_spec_gate: bool = False` to `PhaseManager.transition` (and a `--bypass-spec-gate` option on `cli/phase.py`'s phase command, threaded through).
  - Implement a helper `_has_approved_spec(task_slug) -> bool` in `phase.py` that scans `<workspace_root>/specs` via `SpecStore` for a spec with matching `task_slug` and an approved-or-later status. (Reuse `SpecStore` + `SPECS_DIRNAME`.)
  - In `transition`, when `current=="plan"` and `target=="dev"` and `not bypass_spec_gate` and `not _has_approved_spec(task_slug)` → raise a `SpecGateError` (new, or reuse an existing phase error type) with a clear message naming `--bypass-spec-gate`.
  - 🟡 The existing soft "no spec found" *warning* (`_gate_dev`) stays for the bypass path / informational; the new behavior is the hard block. Adjust the two existing gate tests (`test_gate_dev_satisfied_by_blessed_path`, `test_gate_dev_hint_mentions_spec_new`) to pass `--bypass-spec-gate` (or seed an approved spec) so they still exercise the warning path without the new block.
  - Tests: `plan→dev` raises without an approved bound spec; succeeds with one seeded (`SpecStore.save` a spec with `task_slug` + `status="approved"`); succeeds with `--bypass-spec-gate`.
- [ ] **Commit:** `feat(spec): require an approved spec for plan→dev (--bypass-spec-gate)` + journal.

---

## Task A6 — `mship spec dispatch` 🟡 (integration-risk)

**Files:** Modify `cli/spec.py`; test `tests/cli/test_spec_dispatch.py` (uses a git-backed workspace fixture from `tests/conftest.py`, e.g. `workspace_with_git` / `audit_workspace`).

- [ ] **Read first:** `WorktreeManager.spawn(description, repos, slug, workspace_root, ...) -> SpawnResult` (`core/worktree.py:396`); `build_dispatch_prompt(task, repo, instruction, journal_entries, base_sha_info, agents_md_path, pkg_skills_source, state)` and how `cli/dispatch.py` calls it; the `container.worktree_manager()` + `container.state_manager()` providers.
- [ ] **Implement `spec dispatch <id>`** (TDD):
  1. Load spec; require `spec.status == "approved"` (else error "approve it first").
  2. `result = container.worktree_manager().spawn(description=spec.title, repos=(spec.affected_repos or None), slug=spec.id, workspace_root=Path(container.config_path()).parent)`.
  3. State mutate: set the new task's `spec_id = spec.id`. Set `spec.status = "dispatched"` (`validate_transition`), `spec.task_slug = result.task.slug` (or the spawned slug), save the spec.
  4. Emit a handoff prompt via `build_dispatch_prompt` seeded with `instruction = spec problem + acceptance criteria` (build the instruction string from `parse_body_sections(spec.body)["Problem"]` + the criteria texts). Print to stdout.
  - Tests (git fixture): from an approved spec → `spec dispatch dq` exits 0, a task exists with `spec_id == dq`, `spec.status == "dispatched"`, output contains an acceptance-criterion text; dispatching an unapproved spec errors.
- [ ] **🟡 Fallback (if the full spawn integration proves too entangled to test cleanly):** implement `spec dispatch <id>` to require an *already-spawned* task whose `slug == spec.id` (or `--task`), bind `task.spec_id`/`spec.status=dispatched`, and emit the prompt — **no auto-spawn**. Document this fallback prominently in the PR body if taken.
- [ ] **Commit:** `feat(spec): spec dispatch (approved spec → task + handoff prompt)` + journal.

---

## Final steps
- [ ] **Full suite:** `mship test` → green.
- [ ] Final whole-branch review (per subagent-driven-development), then `mship finish` → one PR.

## Self-Review
- **Coverage:** A4 (questions) → core+CLI; A5 (approve/request-changes) → `approval_blockers` + CLI; A7 (gate) → `transition` hard-block + bypass; A6 (dispatch) → spawn + bind + prompt (or fallback).
- **Placeholders:** A4/A5/A7 have complete code/contracts; A6 is directed (read-then-mirror the spawn machinery) by necessity (integration), with an explicit fallback — flagged, not a hidden gap.
- **Type consistency:** reuses `Spec`/`OpenQuestion`/`AcceptanceCriterion`/`validate_transition`/`InvalidTransition`/`SpecStore`/`SPECS_DIRNAME`/`parse_body_sections`/`build_dispatch_prompt`/`WorktreeManager.spawn`. New: `add_question`/`answer_question`/`list_questions`, `approval_blockers`, `_has_approved_spec`.

## Execution Handoff
Combined branch; commit this plan + design to `main`, then `mship spawn` MOS-148–151 as one task.
