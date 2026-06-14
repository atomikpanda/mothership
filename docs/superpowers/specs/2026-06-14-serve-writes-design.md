# `mship serve` write endpoints (review → approve) — design (B1.5 / MOS-166)

> Status: design approved 2026-06-14. Feeds `writing-plans`.
> Part of **Ground Control** (epic MOS-144). Adds the review/approve write loop to
> the `mship serve` API (B1 read-only + B2 auth, both merged).

## Context

B1 serves read-only; B2 gates it. B1.5 adds the **POST** endpoints the C4 review
cards drive — submit verdicts, manage questions, approve / request-changes — so a
reviewer can act from their phone. Each is a thin wrapper over an existing core
write helper (no logic fork from the CLI).

## Decisions

1. **Endpoints (all POST, all on the existing app):**
   | Endpoint | Body | Core call |
   |----------|------|-----------|
   | `POST /specs/{id}/verdict` | `{criterion_id, verdict}` | `set_criterion_verdict` |
   | `POST /specs/{id}/questions` | `{text}` | `add_question` |
   | `POST /specs/{id}/questions/{qid}/answer` | `{answer}` | `answer_question` |
   | `POST /specs/{id}/approve` | `{bypass_gate=false}` | `approval_blockers` + `validate_transition` |
   | `POST /specs/{id}/request-changes` | `{reason}` | `validate_transition` |
2. **Request bodies are pydantic models** (FastAPI validates + schemas them; malformed body → `422` automatically).
3. **Every write returns `build_review(spec)`** — the fresh review payload (status + criteria + questions + summary), so the app refreshes its cards in one round-trip.
4. **Each handler:** `find_by_id` (→ `404` if absent) → call the core helper → bump `updated_at` → `SpecStore.save` → return `build_review`. (Same load→mutate→save the CLI does.) `request-changes` also best-effort journals the reason via `log_manager` (if present).
5. **Error → HTTP mapping:**
   - spec not found → **404**
   - core `ValueError` (bad verdict value, unknown criterion/question id, prose-unit id) → **400** (message passed through)
   - `InvalidTransition` (approve/request-changes from the wrong status) → **409**
   - approval gate blocked (not bypassed) → **409**, body lists the blockers
   - malformed JSON body → **422** (FastAPI default)
6. **Auth: inherited.** Writes are routes on the same app, so the B2 app-level bearer dependency already gates them — no extra auth code. (A test asserts a write `401`s without the token.)

## Scope (B1.5)

In: the 5 review/approve write endpoints + their request models + error mapping.
Out: capture writes (`POST /specs`, `/draft`, `/apply`), `dispatch` over HTTP
(spawn follow-up), idempotency keys, optimistic-concurrency (single-user; SpecStore
atomic writes suffice).

## Migration / compat

Additive — new POST routes on the existing read-only app; GET behavior, auth, and
the docs-disabled-under-auth behavior unchanged.

## Testing

- **verdict:** sets a criterion's verdict (response `build_review` reflects it); bad
  verdict value → `400`; unknown criterion → `400`; unknown spec → `404`.
- **questions:** add → review shows the new `q`; answer → review shows the answer;
  unknown `qid` → `400`.
- **approve:** blocked (unreviewed criterion / unanswered question) → `409` with
  blockers; after verdict+answer → `200`, status `approved`; `bypass_gate=true` →
  `200`; wrong status (re-approve) → `409`.
- **request-changes:** → `200`, status `needs_clarification`.
- **auth:** with `auth_token` set, a write with no `Authorization` header → `401`
  (confirms writes inherit the B2 gate).
