# Specs and plans across machines

Two questions come up as soon as a workspace exists on more than one machine — say
a Linux devbox where you write code and a desktop run host that owns the iOS
simulator:

1. Do I commit specs and implementation plans, or are they scratch?
2. If I commit them, what keeps them in step between machines?

Short answers: **yes, commit them**, and **plain `git` on the workspace repo** —
no mship command syncs them for you, on purpose.

## Where they live

Specs and plans are **workspace** artifacts, not repo artifacts:

```
my-workspace/                 ← a git repo of its own
├── mothership.yaml
├── specs/2026-07-25-my-feature.md
├── docs/plans/2026-07-25-my-feature.md
├── .mothership/              ← gitignored: machine-local runtime state
├── .worktrees/               ← gitignored: task checkouts
├── api/                      ← a member repo, tracked in ITS own git
└── web/                      ← likewise
```

The workspace repo tracks the config, the specs, and the plans. Member repos are
excluded from it (each is its own repository) and so is everything under
`.mothership/`.

## Commit them

`spec_storage` defaults to `committed`, which is the mode you want unless you have
a specific reason not to:

```yaml
# mothership.yaml
spec_storage: committed      # the default
```

| Mode | On disk | Travels between machines? |
|---|---|---|
| `committed` | `specs/*.md`, tracked | Yes |
| `local` | `specs/*.md`, gitignored | **No** — stays on the machine that wrote it |
| `encrypted` | `specs/*.md.enc`, tracked | Yes, but only *renders* where the key is |

Plans have no equivalent setting — they are ordinary files under
`docs/plans/`, and you commit them.

There is a functional reason to commit plans, not just a tidiness one: for a
`feature` work item, `mship phase dev` requires a linked plan and resolves
`plan_path` **against the workspace root**. A plan that only exists in a task
worktree, or only on one machine, is a plan the gate cannot find — so `phase dev`
on your second machine will refuse to proceed.

```bash
git add specs/ docs/plans/ mothership.yaml
git commit -m "spec + plan: my-feature"
git push
```

## Keeping two machines in step

`mship sync` fast-forwards **member repos** — it deliberately does not touch the
workspace repo, because that repo holds your config and your specs and mship will
not rewrite those behind your back. So the loop is ordinary git:

```bash
# on the machine that wrote the spec
git add specs/ docs/plans/ && git commit -m "spec: my-feature" && git push

# on the other machine
git pull                # specs + plans arrive
mship sync              # member repos fast-forward
mship spec list         # the spec is now visible here
```

A spec you have not committed simply does not exist on your other machine. If
`mship spec list` looks short there, check `git status` on the workspace — a pile
of untracked files under `specs/` is the usual cause.

## What deliberately does *not* sync

`.mothership/` is gitignored, and everything in it is per-machine by design:

| State | Why it stays local |
|---|---|
| `state.yaml` — tasks, phases, worktrees | A task's worktrees are paths on *this* disk |
| `serve-token` | Each machine's serve has its own bearer |
| `run-hosts.yaml` — role → url + token | Which machines *this* one can reach, plus their tokens |
| `relay-runtime.json` | The tunnel this machine is running |
| `messages/`, `workitems/` | Mailbox and work-item stores for this machine's serve |

The consequence worth internalising: **tasks are machine-local**. A task you
spawned on the devbox does not exist on the desktop — `mship status` there will
not list it, and `mship test --task <slug>` will report an unknown task. What
crosses machines is the *branch* (pushed to the remote) and the *spec and plan*
(committed to the workspace repo). To continue a task elsewhere, spawn a task
there against the same branch, or use `--remote` to run on the other machine from
the one holding the task.

## Running one machine's work on another

For the iOS-simulator case you usually do not want a second copy of the task at
all — you want the desktop to execute one step:

```bash
mship run --remote=desktop        # runs on the desktop, streams back here
```

That materialises the task's branch in a worktree on the run host, so the code
arrives via git rather than via the workspace repo. See
[Remote run hosts](../remote-run.md) for setting the role up.

## If a spec should not be committed

Use `local` for a spec that must not leave the machine, or `encrypted` for one that
should travel to your other machines but stay unreadable in the remote:

```yaml
spec_storage: encrypted
```

Encrypted specs are committed as `.md.enc`. Any machine without the key still
sees the spec listed — Ground Control shows it as `LOCKED` rather than leaking
ciphertext — but cannot render its contents. That is the mode to pick when you
want cross-machine sync without putting spec text in a repository.

Switching modes later is a migration, not just a config edit:

```bash
mship spec migrate-storage      # re-materialise every spec into the current mode
```
