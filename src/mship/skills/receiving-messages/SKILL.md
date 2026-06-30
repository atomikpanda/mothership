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
   `mship reply <thread-id> "<your answer>"`.
3. **Re-arm** with the returned cursor: `mship inbox wait --since <cursor> --timeout 50`.
   The `--since` cursor means you never re-wake for a message you already handled
   (or for your own reply).
4. On `timed_out: true`, just re-arm again.

Never spawn a new agent / `claude -p` — this is all in your existing session.
