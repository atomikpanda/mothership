# The Unattended Cloud Runner — Workflow & Setup

An unattended cloud runner is a **scheduled Claude Code routine that becomes a
disposable mship worker**: when it fires it clones the workspace, implements
**one approved spec**, and opens the PR(s) — all routed through the **relay**,
which attaches and enforces the real GitHub credential at egress. The worker
never holds a GitHub token; it carries only a low-value, short-lived **per-run
token**. Nothing auto-merges — a human reviews and merges in the morning.

This doc is the **operator entry point**: the end-to-end workflow, plus the
one-time go-live setup. It stitches together three deep-dive docs and one skill;
each is linked at the point it becomes relevant, and their internals are not
restated here.

- **`cloud-worker-auth-spine.md`** — the attach-at-relay credential egress proxy
  (trust model, enforcers, module boundary).
- **`cloud-agent-auth.md`** — the simpler `/gh-token` broker (the daytime/trusted
  variant) and the GitHub App setup that both models share.
- **`adapters/claude-routine-runner.md`** — the alternative pull-API host model
  (`mship item run-next` selects + claims from a backlog).
- **skill `overnight-cloud-worker-routines`** — the agent-facing pattern for
  minting the token and standing up the routine.

---

## Mental model: three planes

| Plane | Who | Responsibility |
|---|---|---|
| **Control** | `mship` | Spec approval, WorkItem state, the `finish` gates (approved spec + passing tests + audit). Host-agnostic — mship never spawns an agent. |
| **Execution** | the Claude Code routine | The disposable worker: clone → implement one spec → open PR(s). Swappable for any scheduler that can run shell + invoke an agent. |
| **Credential** | the relay egress proxy | Attaches the repo-scoped GitHub App token **at egress** and enforces the run's scope (which repos, which push branch). The worker never sees it. |

The recommended path below is **attach-at-relay** (worker holds no credential),
the model built for untrusted, prompt-injectable overnight workers. Two simpler
variants — the `/gh-token` broker and the pull-API runner — are covered under
[Variants](#variants--when-to-use-them).

---

## One-time setup (operator go-live checklist)

Done once; does **not** repeat per run. All the code these steps drive is
shipped — this is deployment, not development.

### A. GitHub App — the credential source

The App mints short-lived, repo-scoped installation tokens without any dev
machine being awake, and one App can span every account/org you own.

- Create the App (GitHub → Settings → Developer settings → GitHub Apps → New):
  **Contents** = Read & write, **Pull requests** = Read & write (nothing else),
  and **Where can this app be installed = Any account**.
- **Install** it on every account/org whose repos your workspaces touch.
- **Download the private key** (`.pem`) — GitHub lets you download it once.
- Put the creds on the **egress host** (key under a gitignored path, never in
  git):

  ```bash
  MSHIP_GH_APP_ID=<app-id>
  MSHIP_GH_APP_KEY=/path/to/app.private-key.pem   # file PATH, not the PEM text
  ```

There is **no installation id to set** — the egress proxy resolves the
installation per repo from the App key. Full detail: `cloud-agent-auth.md` §2.

### B. Relay egress proxy — the credential-attaching front door

Front the egress proxy with Caddy and run it with the App creds in its env:

```bash
# Caddy: egress.<RELAY_DOMAIN> -> 127.0.0.1:47280 (on-demand TLS;
# the `egress` label is allow-listed in tls_ask).

MSHIP_GH_APP_ID=<app-id> MSHIP_GH_APP_KEY=/path/to/app.pem \
  mship relay egress-server \
    --grant-store-dir  ./grants-store \
    --run-token-dir    ./run-tokens-store
```

With **no** App creds the egress-server **fails closed** — every request returns
503, it never forwards unauthenticated. Full detail: `cloud-worker-auth-spine.md`
§5. The `api.github.com` leg rides the same subdomain (no extra route/TLS): §8.

### C. Enroll a worker identity + set its ceiling

```bash
# The worker device requests access; you approve it (existing enroll flow):
mship relay enroll          # from the worker device — requests relay access
mship relay requests        # on the relay host — list pending (id · host · fp)
mship relay approve <request-id>

# Set the CEILING — the repos this enrollment may EVER touch (superset of any
# single run's repos):
mship relay grant <enrollment-id> --provider github-app \
  --repos owner/workspace-repo,owner/member-a,owner/member-b
```

Verify auth actually covers every repo **before** trusting the setup:

```bash
mship gh preflight
# ✓ auth OK — broker covers: owner/member-a, owner/member-b, ...
```

`gh preflight` is strict by design — it fails fast and names the exact repo the
App isn't installed on, so a run never burns AI tokens on code it then can't
push. Detail: `cloud-agent-auth.md` §4.

---

## Per-run lifecycle (once per approved spec)

### 1. Approve the spec

Normal flow: brainstorm → `mship spec` → review → `approve`. The runner
implements exactly one approved spec per routine. Keep it review-gated — do not
wire any auto-merge.

### 2. Mint a per-run token (scoped to this run)

```bash
mship relay issue-run-token <enrollment-id> \
  --repos "owner/workspace-repo,owner/member-a" \
  --push-branch "feat/<slug>" \
  --ttl 86400
```

- `--repos` ⊆ the enrollment's ceiling. **Must include the workspace repo** (so
  the worker can clone the workspace to read the spec/plan) **and every affected
  member repo** (to clone, push, and open PRs).
- `--push-branch` is the only branch the attached credential may push.
- The token prints **once**. Inject it into the routine's environment as the
  `--run-token` value. It is low-value: scoped, short-lived, useless without the
  relay.

### 3. Schedule the Claude Code routine

Scheduling and creating a routine is **agent behaviour** (your own Claude Code
`/schedule` ability), **not** mship code — mship does not automate it. Give the
routine an environment that installs `mship` and carries the relay URL + the run
token, and a prompt naming the spec to implement. See the
`overnight-cloud-worker-routines` skill for the exact pattern.

### 4. The worker (inside the fired routine)

```bash
# a. Clone the workspace repo through the relay, then from its root:

# b. Configure git for the relay and clone members with NO GitHub token:
mship bootstrap --relay-url "<relay-url>" --run-token "<run-token>"

# c. Fail-fast BEFORE spend — verify relay-routed auth can actually push:
mship gh preflight --relay-url "<relay-url>" --run-token "<run-token>"

# d. Implement the assigned spec (normal mship phase workflow).

# e. Open the PR(s) — routed through the relay by the git config bootstrap wrote:
mship finish          # opens PR(s); NEVER merges, NEVER pushes the base branch
```

### 5. Morning: review + merge

You review and merge in Ground Control (the Queue / Review cockpit). This is the
only merge step — the runner always stops at an open PR.

---

## Guarantees (the security posture, in one place)

- **The worker holds no GitHub credential.** No App key, no broker bearer, no
  installation token — only the low-value per-run token. Presented directly to
  `github.com` that token is rejected; presented to the relay it unlocks only a
  **run-branch push to the run's repos**.
- **The minted App token is repo-scoped and short-TTL**, and its Attachment is
  **host-locked** to `github.com` / `api.github.com` — a route misconfig cannot
  send it anywhere else.
- **The git leg enforces run-branch-only push** (clone/fetch pass; other
  branches, other repos, and branch *deletes* are refused).
- **The REST leg is default-deny, PR-open only.** The worker may `POST .../pulls`
  (open its PR) and do scoped reads; every write to an existing PR/issue, every
  ref/content mutation, and **every merge** is refused. Nothing auto-merges — the
  fan-out is review-gated end to end (#393).

---

## What's shipped vs what you set up

- **Shipped (code, 2026-07-22):** `bootstrap`/`gh preflight` `--relay-url` +
  `--run-token`; `mship relay grant` / `issue-run-token` / `egress-server`; the
  git-receive-pack run-branch enforcer and the default-deny REST enforcer; the
  `overnight-cloud-worker-routines` skill.
- **Operator go-live (once):** create + install the GitHub App and put its creds
  on the egress host (A); deploy the Caddy egress block and run `egress-server`
  (B); enroll + grant a worker identity (C). Then, per run: mint a token and
  schedule the routine.

---

## Variants & when to use them

- **Daytime / trusted, zero App — the `/gh-token` broker.** If a workspace only
  runs while your own machine is awake, skip the App entirely: `mship serve`'s
  `/gh-token` proxies the host's `gh auth token`, and the worker pulls a
  short-lived token itself. Simpler, but single-identity and needs a machine
  awake + logged in. Detail: `cloud-agent-auth.md` §3.
- **Pull-API runner — `mship item run-next`.** Instead of scheduling one routine
  per named spec, let mship pick the next eligible item from a backlog: it
  selects the oldest `unattended` + `ready` + approved item, claims it
  (git-backed), and emits a dispatch prompt; the host runs one item per tick and
  `mship item bail`s on an unresolvable fork. Use when you want backlog-draining
  rather than per-spec scheduling. Full contract + smoke test:
  `adapters/claude-routine-runner.md`.

---

## See also

- `cloud-worker-auth-spine.md` — attach-at-relay egress proxy (trust model, seams, enforcers).
- `cloud-agent-auth.md` — GitHub App setup + the `/gh-token` broker variant.
- `adapters/claude-routine-runner.md` — the pull-API host model + smoke test.
- skill `overnight-cloud-worker-routines` — the agent-facing routine pattern.
- specs: `cloud-worker-auth-spine`, `worker-pr-egress`, `relay-aware-worker-boot`, `cloud-agent-github-auth`, `unattended-runner`.
