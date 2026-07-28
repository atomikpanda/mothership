# VENDOR.md — vendored-skills delta ledger

**Upstream base:** `obra/superpowers v6.2.0` (commit `3dcbd5c4b48e02263fbf4a3c01e3fe4f81d584d9`)
**Vendored:** 2026-07-28 (spec `re-vendor-superpowers-620-with-mship`, mothership #437/#439)
**Drift guard:** `tests/skills/test_vendor_ledger.py` + `.upstream-manifest.json` (sha256 of every vendored upstream file). Any file that diverges from upstream without an entry here fails CI — update this ledger when you modify a vendored file.

Delta vocabulary (the spec's four kinds):
1. **namespace stripping** — residual upstream skill-namespace (`superpowers`) prefix / path de-branding upstream's vendor-neutral rewrite missed.
2. **mship routing** — worktree/finish/brainstorm/plan flows routed through the `mship` CLI when a `mothership.yaml` workspace is detected.
3. **mship subagent anchoring** — SDD/dispatch flows rebuilt on `mship dispatch` v2 (stub + subagent-side `--emit`, reviewer packages, dispatch-resolved models).
4. **mship debug integration** — `mship debug` hypothesis/rule-out/resolved trail + `mship test` evidence.
5. **security patch** — minimal local fixes for security findings in vendored upstream code (CodeQL/review-blocking), kept as small, ledgered diffs pending upstreaming.

## Structural exclusions (upstream content deliberately NOT vendored)

- **`skills/subagent-driven-development/scripts/`** — upstream's three SDD shell scripts are not vendored:
  - `subagent-driven-development/scripts/review-package`
  - `subagent-driven-development/scripts/sdd-workspace`
  - `subagent-driven-development/scripts/task-brief`

  Their roles (workspace scratch dir, task briefs, review packages under the upstream `.superpowers` sdd scratch dir) are replaced by `mship dispatch` v2 (#439): structured store under `.mothership/sdd/`, pointer-stub dispatch, `mship dispatch --emit` in the subagent, `--mode reviewer` packages.
- **`skills/using-superpowers/`** (except `references/`) — not vendored; `using-mothership` is its role-equivalent. Its `references/*` ARE vendored, relocated to `using-mothership/references/` (the manifest records them at the relocated path).

## Local-only files

- `SUPERPOWERS_LICENSE` — upstream's MIT license text, kept alongside the vendored tree (upstream ships it as repo-root `LICENSE`, outside `skills/`). Version attribution lives in `THIRD_PARTY_LICENSES.md`.
- `using-mothership/`, `working-with-mothership/`, `overnight-cloud-worker-routines/`, `receiving-messages/` — mship-original skills, no upstream counterpart (guard treats them as originals).

## Modified skills

### brainstorming

- `brainstorming/SKILL.md` — kind 2 (mship routing): design capture is dual-path on workspace detection — in a mothership workspace the design becomes an `mship spec` (new/draft/apply → needs_review, workspace-level and branch-stable), outside one a plain `docs/specs/` design doc. Flowchart and review-gate messages updated to match. Also kind 2: on in-chat spec approval the agent records it on the operator's behalf (`spec verdict` per criterion + `spec approve`; review-gate message offers this path), and a subagent-exemption note (dispatched implementers on an already-approved spec/plan skip the design gate). Chat-approval recipe hardened (PR #443 review): check `spec review`/`spec questions` first, answer open questions and re-verdict previously-flagged criteria (explicit chat approval supersedes the earlier flag) before `spec approve`; previously-flagged prose sections clear via the same `mship spec verdict <id> <section> approved`; `--bypass-gate` stays operator-only and Request-changes stays untouched.
- `brainstorming/spec-document-reviewer-prompt.md` — kind 2: dispatch-after condition rewritten for the dual-path capture (`mship spec review <id>` in a workspace, `docs/specs/` doc otherwise); drops the upstream `docs/superpowers/specs/` path.
- `brainstorming/scripts/server.cjs` — security patch: (a) CodeQL js/reflected-xss alert #37 — the `/?key=` bootstrap reflected the request's key into a `<script>` body; now escapes `<`/`>`/`&` as `\uXXXX` in the JSON literal and reflects the server's own `TOKEN` instead of the request copy; (b) cleartext key transport — refuses to bind a non-loopback host (the session key rides plain http/ws URLs) unless BOTH `BRAINSTORM_ALLOW_NON_LOOPBACK=1` AND an https `BRAINSTORM_PUBLIC_URL` (the operator's TLS front) are set; all keyed access URLs are built by a single `buildAccessUrl()` owner, which under the override emits only the https public URL — never the raw host:port — so the key rides TLS even on the initial page load, and browser-origin acceptance is owned by one `allowedOrigins()` helper — same-origin `http://<Host>` on loopback (upstream behavior) plus, under the override only, exact string equality with the `BRAINSTORM_PUBLIC_URL` origin (no wildcards) so proxied wss upgrades aren't destroyed (Origin never carries a path, so a path-mounted `BRAINSTORM_PUBLIC_URL` needs no origin change), and the bootstrap page reloads `location.pathname` instead of the origin root so a path-mounted proxy stays inside its route; (c) undisclosed third-party fetch — the primeradiant.com brand image is now opt-in via `SUPERPOWERS_ENABLE_REMOTE_BRANDING`; default render makes no external request (local attribution text instead).
- `brainstorming/scripts/helper.js` — security patch (cleartext key transport): the browser helper attaches the session key to ws/URL navigations only when the page is loopback or served over https (then wss); otherwise it connects keyless and logs a clear use-loopback-or-TLS error instead of leaking the bearer key over the network. All its URLs are same-origin/relative, so under the server's https `BRAINSTORM_PUBLIC_URL` front they inherit TLS and the guard permits the key; WebSocket/recovery URLs build on the page's own path base (pure `pathBase()`), not the origin root, so path-mounted reverse proxies (e.g. `https://host/companion`) work as well as root-mounted ones.
- `brainstorming/scripts/start-server.sh` — de-branding: visual-companion session/port/token files live under `.mothership/brainstorm/` instead of the upstream `.superpowers` brainstorm dir (the last upstream-scratch-dir paths in the tree); security patch: usage comment documents that non-loopback binds require the two-var override (`BRAINSTORM_ALLOW_NON_LOOPBACK=1` + https `BRAINSTORM_PUBLIC_URL`) introduced in server.cjs.
- `brainstorming/scripts/stop-server.sh` — same upstream-scratch-dir → `.mothership/` re-path in the persistence comment.
- `brainstorming/visual-companion.md` — same `.mothership/brainstorm/` re-path in examples, connection-info notes, and the gitignore reminder; security patch: the remote/non-loopback launch example rewritten to the two-var override flow (`BRAINSTORM_ALLOW_NON_LOOPBACK=1` + https `BRAINSTORM_PUBLIC_URL`, key travels only over the TLS front); loopback default example unchanged and primary.

### dispatching-parallel-agents

- `dispatching-parallel-agents/SKILL.md` — kind 3 (subagent anchoring): appended "Mothership Workspace" section — confirm an anchored task via `mship status` and set each agent's cwd to its task worktree (`.resolved_task.worktrees.<repo>`); agents on `main` are blocked by the pre-commit hook. Rewritten (intent-scan): parallel implementers on one task/worktree are forbidden (SDD rule); the working parallel patterns are read-only agents sharing a worktree or one task per agent (separate `mship spawn`, pinned via `MSHIP_TASK`/`--task`); points at working-with-mothership's multi-task section.

### executing-plans

- `executing-plans/SKILL.md` — kind 2 + residual kind 1: anchored-task precondition inserted (verify `mship status` resolves a task, WorkItem-first `mship item new` → `mship spawn --work-item`, work from the task worktree, spec-gate note) and finishing routed via `mship finish --body-file`; remaining upstream skill-namespace (`superpowers`) prefixes dropped. Spec-gate note corrected (intent-scan): feature-kind WorkItems always hard-block at `phase dev` (kind gate); `require_approved_spec` is the legacy opt-in layer; bypass replaced with stop-and-ask-the-operator. Unconditional "use subagent-driven-development instead" softened to honor an explicit inline-execution choice.

### finishing-a-development-branch

- `finishing-a-development-branch/SKILL.md` — kind 2: PR path replaced by `mship finish --require-tests --body-file` (push+PR+state stamp, evidence-gated) with post-finish iteration via `mship commit`; Step 1 verification runs `mship test` (not a bare runner) so finish's evidence gate has something to read; merge path corrected (intent-scan + PR #443 review) — Option 1 is not the normal workspace path (Option 2 + merge auto-advance is); the sanctioned local-merge-no-PR sequence is `mship finish --push-only` → merge EVERY affected repo's branch into its base → plain `mship close`, in that order (close verifies each repo's merge reached its base and only then advances spec/WorkItem like a merged close; a partially-merged close prompts before teardown and does not advance; closing before any merge is safe but advances nothing), with a runnable per-repo merge block driven by `mship status` jq state (worktrees/branch/base — upstream's single-repo block kept for non-workspace use), never `close --abandon` for delivered work; discard path routes through `mship close --abandon`. Non-workspace flows keep upstream's forge tooling.

### requesting-code-review

- `requesting-code-review/SKILL.md` — de-branding: example plan path `docs/superpowers/plans/` → `docs/plans/`. Kind 3 (intent-scan re-scope): SDD flows own their per-task and final reviews (package-based, `mship dispatch --mode reviewer` + SDD prompts); this skill covers ad-hoc/mid-task reviews outside that flow — the "after each SDD task" mandatory trigger removed; worked example's verdict aligned to the template's "Ready to merge?" vocabulary.
- `requesting-code-review/code-reviewer.md` — kind 3: optional `[REVIEW_BUNDLE_PATH]` placeholder — when a bundle/package path is provided the reviewer reads diff files from disk instead of re-running git (reconciles SDD's final-review usage); git instructions kept for the no-bundle case.

### subagent-driven-development

Upstream 6.2.0 restructured this skill around its file-passing scripts; re-woven onto `mship dispatch` v2 keeping the methodology (single dual-verdict task reviewer, whole-branch final review, controller-coaching ban, read-only reviewers).

- `subagent-driven-development/SKILL.md` — kind 3: pre-dispatch `mship status` envelope checks (empty `active_tasks` → WorkItem-first stop; unanchored multi-task → pick and `--task`), worktree-only work contract, cross-reference to `working-with-mothership`'s authoritative CLI contract.
- `subagent-driven-development/implementer-prompt.md` — kind 3: dispatch via `mship dispatch --task <slug> --plan-task <N>` stub; subagent's first command is `mship dispatch --emit`; model comes from the stub's CLI-resolved line (never controller-chosen); non-workspace fallback preserved.
- `subagent-driven-development/task-reviewer-prompt.md` — kind 3: reviewer dispatched from an `mship dispatch --mode reviewer` package (per-repo raw diffs + `manifest.json` + skipped-repo disclosure, diffed from recorded base to live HEAD); the reviewer's own `--emit` prints the package and the read-only dual-verdict contract; the contract names the sanctioned red-verify method (`git show <sha>:<path>` into a temp copy, never `git stash`/`checkout`) (PR #443 review).
- `subagent-driven-development/re-review-prompt.md` — kind 3: re-review re-runs `--mode reviewer` (package re-diffs to new HEAD) with scope narrowed to the fix range; cheap-tier model guidance defers to the stub's resolved model.
- `subagent-driven-development/scripts/review-package` — deleted (structural exclusion above; replaced by `mship dispatch --mode reviewer`).
- `subagent-driven-development/scripts/sdd-workspace` — deleted (replaced by the `.mothership/sdd/` dispatch store).
- `subagent-driven-development/scripts/task-brief` — deleted (replaced by pointer-stub dispatch + `mship dispatch --emit`).

### systematic-debugging

- `systematic-debugging/SKILL.md` — kind 4 (mship debug) + residual kind 1: appended REQUIRED "mship integration" section — `mship debug hypothesis` / `rule-out` / `resolved` at each methodology checkpoint for a durable audit trace; two upstream skill-namespace (`superpowers`) prefixes dropped.

### test-driven-development

- `test-driven-development/SKILL.md` — kind 4: run tests via `mship test` (records the evidence `mship finish --require-tests` checks); open debug threads auto-attach `parent=<hypothesis-id>` to test-run journal entries, cross-referencing `systematic-debugging`.
- `test-driven-development/writing-good-tests.md` — residual kind 1: one namespace prefix on `writing-skills` dropped.

### using-git-worktrees

- `using-git-worktrees/SKILL.md` — kind 2: "In a Mothership Workspace" section — `mship spawn` is the native worktree tool (WorkItem-first: `mship item new` → `mship spawn --work-item`), setup steps subsumed by spawn, cleanup via `mship close`; decision-table row added.

### using-mothership/references

Vendored from upstream `using-superpowers/references/` (see structural exclusions). One file diverges:

- `using-mothership/references/gemini-tools.md` — residual kind 1: two deliberate upstream skill-namespace (`superpowers`) prefix-drop lines in the dispatch-mapping examples (`subagent-driven-development`, `requesting-code-review`).

### verification-before-completion

- `verification-before-completion/SKILL.md` — kind 4: "Mothership Workspace" section — verify via `mship test` so the result is recorded as evidence for the `mship finish --require-tests` gate; controller-role paragraph added (intent-scan) — in subagent-driven flows the controller may accept a subagent's reported `mship test` evidence once a reviewer verified the work; own-work claims still need fresh self-run evidence.

### writing-plans

- `writing-plans/SKILL.md` — kind 3 + kind 2 + residual kind 1: plan input is an approved `mship spec` (id in the plan header, `mship phase dev` gate); save path parametrized on `docs_dir` from `mship context`; `<!-- mship:task id=N -->` anchors around plan tasks for `--plan-task` dispatch; commit steps paired with `mship journal`; remaining upstream skill-namespace (`superpowers`) prefixes dropped. Plan-gate warning added at the save-location line (intent-scan): filename must stem-match the task slug or be linked via `mship item link-plan`, else the feature plan gate hard-blocks; canonical finish reference now `mship finish --require-tests`.

### writing-skills

- `writing-skills/SKILL.md` — kind 2 + residual kind 1: bundled-skill location (`src/mship/skills/<name>/SKILL.md`, distributed via `mship skill install`) noted; deployment checklist gains the `mship skill install` propagation step; reference links re-pointed `using-superpowers/references/` → `using-mothership/references/`; upstream skill-namespace (`superpowers`) prefixes dropped.
- `writing-skills/testing-skills-with-subagents.md` — residual kind 1: one namespace prefix on `test-driven-development` dropped.
