---
name: receiving-messages
description: Use to receive and answer durable phone messages (the mship mailbox) while a session is idle — keep a background `mship inbox wait` armed and re-arm after each reply.
---

# Receiving Messages

The phone sends durable messages to this workspace's mailbox (`mship inbox`). Two
mechanisms surface them to a live agent (one serve + one agent per workspace):

- **Mid-turn:** the `Stop` hook (`mship _drain`) drains the inbox at each turn
  boundary automatically — you don't have to do anything.
- **While idle:** keep a long-poll armed so you wake when a message lands.

## The idle arm/re-arm loop

1. When you finish your work and would otherwise go idle, run **in the
   background**: `mship inbox wait --timeout 50` (like backgrounding a test run).
   It blocks until a new *human* message arrives (or it times out), then returns
   JSON `{threads, cursor, timed_out}` and your harness re-invokes you.
2. On wake with `threads`, answer each and clear it:
   `mship reply <thread-id> "<your answer>"`. If the pending line isn't enough
   context, read the whole conversation first with `mship messages <thread-id>`.
3. **Re-arm** with the returned cursor: `mship inbox wait --since <cursor> --timeout 50`.
   The `--since` cursor means you never re-wake for a message you already handled
   (or for your own reply).
4. On `timed_out: true`, just re-arm again.

Never spawn a new agent / `claude -p` — this is all in your existing session.

## Answering: plain reply, needs-you, and decisions

Every agent→operator message is a reply into an **existing** thread (the phone
opens threads; you can't cold-start one). Pick the form by what you need back:

- **Plain answer** (no action needed from the operator):
  `mship reply <thread-id> "<answer>"`.
- **Needs the operator to act** (do a thing, then it's done): add `--needs-you` —
  `mship reply --needs-you <thread-id> "<what you need them to do>"`. This surfaces
  as a **Home action card** in Ground Control instead of a plain chat reply. Use it
  sparingly, for genuine hand-backs (e.g. "approve the deploy", "the base branch
  needs pushing").
- **Need a decision between options:** `mship ask` posts a tappable **decision card**:

  ```bash
  mship ask <thread-id> "Which auth approach?" \
      --option "Session cookies" --option "JWT" \
      --recommend 0            # 0-based index of your recommendation (optional)
      # --no-free-text         # disallow a typed reply — force one of the options
  ```

  Needs ≥2 `--option`s. Prefer `ask` over a prose "A or B?" reply when the answer
  is a choice — the operator taps once and you get a clean, unambiguous response.

Read any thread's full history at any time with `mship messages <thread-id>`
(`--json`-friendly when piped).
