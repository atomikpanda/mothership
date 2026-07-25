# Phone control

**When you need this:** you want to steer the work — approve specs, answer an
agent's questions, review and merge PRs — away from your desk.

mship's phone app is **Ground Control** (Android). The workspace side is just
`mship serve`; everything below rides on it.

## 1. Start serve

```bash
mship serve            # reachable on your LAN / tailnet
mship serve --relay    # reachable from anywhere, via a reverse tunnel
```

`--relay` dials out to a relay host and gets a stable per-device URL, so NAT
and coffee-shop Wi-Fi don't matter. You can use a self-hosted relay
([Relay hosting](../relay-hosting.md)); serving over a tailnet works too
([Serve over Tailscale](../mship-serve-tailscale.md)).

## 2. Pair the phone

```bash
mship pair
```

Prints a `groundcontrol://` deep link and a QR code carrying the serve URL and
token. Scan it in Ground Control and the workspace appears in the app. Repeat
per workspace — the app is multi-workspace.

## 3. What you can do from the phone

- **Capture ideas.** A thought typed on the phone becomes a thread the agent
  can brainstorm with you and shape into a spec — capture is a conversation,
  not a one-shot form.
- **Review and approve specs.** Specs in `needs_review` surface in the
  **Queue** as swipeable cards: acceptance criteria, open questions, verdicts.
  Approve, or request changes with a reason — approval is what unlocks feature
  development ([Ship a feature](ship-a-feature.md)).
- **Answer decisions.** When the agent hits a fork it can't resolve alone, it
  posts a **decision card** — a question with tappable options. One tap
  unblocks the build.
- **Chat with the agent.** Every work item has its threads; messages are
  durable, so the agent answers when it wakes even if it wasn't running when
  you wrote.
- **Watch progress.** Work items show their phase (inbox → shaping → ready →
  in flight → review → done) as the linked spec, tasks, and PRs advance.
- **Review and merge PRs.** Merged PRs flow back as events — the agent sees the
  merge and closes out the task.

## 4. How the agent hears you

Messages land in a durable mailbox on the workspace. A running agent listens
between turns (`mship inbox wait`) and drains the inbox at each turn boundary,
then answers with `mship reply` or posts options with `mship ask`. Nothing is
lost if either side is offline — it's store-and-forward, not a live socket.
