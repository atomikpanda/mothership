# Cloud-worker auth spine — attach-at-relay credential egress proxy (Shape 2)

The overnight cloud-worker fan-out runs disposable, prompt-injectable workers that
must clone/fetch/push across several repos — yet a worker must never hold a GitHub
credential. This spine turns the relay into a scoped, credential-attaching **egress
proxy**: the worker points its git remote at the relay carrying only a low-value
placeholder + per-run token; the relay attaches the real, repo-scoped GitHub App
token at egress and enforces what the worker may do. The credential never lands on
the worker.

Modules: `src/mship/core/relay/egress/*` (proxy role), `src/mship/core/relay/grants.py`
(typed ceiling), `src/mship/core/relay/run_token.py` (per-run token). CLI:
`mship relay grant`, `mship relay issue-run-token`, `mship relay egress-server`.

## 1. Trust model

Three trust tiers, least-trusted first:

- **Worker — least trusted.** Disposable and prompt-injectable. It holds only a
  *placeholder* (a git `insteadOf` URL rewrite — not a secret) and a *low-value
  per-run token*. It never holds a GitHub credential. If the worker is fully
  compromised, the attacker gets the per-run token — and that token, presented
  directly to `github.com`, is rejected (it is not a GitHub bearer); presented to
  the relay it unlocks only a push of the *run branch* to the *run's repos*.
- **Relay / front door — trusted transport (in v1).** Terminates the worker's TLS
  and forwards to the small trusted core. In Shape 2 the front door and the core
  are co-located, so the relay is trusted.
- **Secrets-egress host — the small trusted core.** Does the exchange: verify the
  per-run token → resolve the enrollment ceiling → enforce the request → mint the
  repo-scoped App token → attach it host-locked → forward to GitHub.

Containment rests on four independent limits, so no single slip is catastrophic:
1. the credential is never on the worker (attached only at egress);
2. the git-smart-HTTP **receive-pack enforcer** restricts pushes to the run branch
   for a run-scoped repo (clone/fetch pass; other branches, other repos, and
   branch *deletes* are refused);
3. the minted App token is **repo-scoped and short-TTL** (built-in blast-radius cap);
4. the **Attachment is host-locked** — a route misconfig cannot send the credential
   to any host outside `github.com` / `api.github.com`.

## 2. Attach-at-relay, Shape 2 (co-located)

All three roles run on the single relay the operator already runs. This is the
deliberate v1 simplification: **some host must see the bearer credential in
plaintext to attach it to an outbound request** — that is unavoidable for any
bearer-token scheme. The design does not pretend otherwise; it *minimizes and
isolates* that plaintext exposure to the small egress-proxy core and keeps it off
the worker entirely.

## 3. North star: the untrusted-relay 3-role split

The end-state splits the tiers onto separate hosts: **worker / blind relay /
separate secrets-egress host**. Because the egress-proxy is a **distinct module**
whose worker session *logically terminates at it* — authenticated end-to-end by the
per-run token the module verifies itself, not by "the relay says so" — relocating
that module onto its own host behind a now-blind relay is a **deployment / wiring
change, not a channel rewrite**. The worker config, the token, the seams, and the
enforcer are all unchanged by the move.

**Shape 2 vs Shape 3 is a FORK, not a ladder.** They defend *different* adversaries:

- **Shape 2 (this):** worker is least-trusted; the relay operator is trusted.
- **Shape 3:** the *relay operator* is least-trusted (a blind courier), which needs
  a second, separately-operated secrets-egress component.

You pick the fork by *which* party you distrust — you do not "graduate" from one to
the other. Attach-at-relay **retires seal-to-worker / HPKE**: there is no longer any
need to encrypt a credential *to* the worker, because the worker never receives one.

## 4. The four seams (and how they admit GitLab / static secrets with ZERO worker change)

The proxy core is `route → provider → enforce → attach → forward`. Four seams make
it extensible without touching the worker or the core:

- **`CredentialProvider`** (`egress/provider.py`) — `resolve(identity, grant, request)
  -> Credential`. v1 ships `GitHubAppProvider` (wraps `core/gh_app.py`). Add a
  `GitLabProvider` or `StaticSecretProvider` implementing the same method.
- **`Attachment`** (`egress/credential.py`) — *how* a credential rides on the wire
  plus a host-lock. GitHub App = `Authorization: token <value>` locked to
  github.com/api.github.com. A different provider supplies a different header/lock,
  e.g. `Authorization: Bearer <value>` locked to `api.openai.com`.
- **`RouteTable`** (`egress/routes.py`) — destination host → `{provider, enforcer}`
  as data. Adding a host is a new entry (+ one `/prefix/` in `request.py`, one
  `tls_ask` allowance, one Caddy block) — **no `github.com` special-case in code**.
- **Typed `Grant`s** (`grants.py`) — a new provider scope on the enrollment ceiling.

**Security asymmetry (why the generalization *strengthens* attach-at-relay):** a
GitHub App token has built-in TTL + repo scope, so even a leaked minted token self-
limits. A static third-party API key (OpenAI, etc.) has **no** built-in TTL or scope
— so the off-box egress boundary is its *only* backstop. Generalizing the four seams
to such secrets makes the attach-at-egress boundary more load-bearing, not less.

## 5. Operator setup

```bash
# 1. Approve the worker's enrollment (existing enroll flow).
mship relay approve <enrollment-id>

# 2. Set the CEILING — the repos this enrollment may EVER touch.
mship relay grant <enrollment-id> --provider github-app --repos owner/a,owner/b

# 3. Issue a PER-RUN token (repos ⊆ ceiling, one push branch). Printed ONCE.
mship relay issue-run-token <enrollment-id> --repos owner/a --push-branch feat/<slug>

# 4. Run the egress proxy with the App creds in its env (refuse-on-unreadable key).
MSHIP_GH_APP_ID=<app-id> MSHIP_GH_APP_KEY=/path/to/app.pem \
  mship relay egress-server --grant-store-dir ./grants-store --run-token-dir ./run-tokens-store
```

Caddy fronts `egress.<RELAY_DOMAIN>` → `127.0.0.1:47280` (on-demand TLS; the
`egress` label is allow-listed in `tls_ask`). With no App creds the egress-server
**fails closed** — every request returns 503, it never forwards unauthenticated.

## 6. Worker config (the placeholder — holds NO usable GitHub credential)

```bash
# URL rewrite (the "placeholder" — NOT a secret): git resolves
#   https://github.com/<owner>/<repo>.git -> https://egress.<RELAY_DOMAIN>/gh/<owner>/<repo>.git
git config --global url."https://egress.<RELAY_DOMAIN>/gh/".insteadOf   "https://github.com/"
git config --global url."https://egress.<RELAY_DOMAIN>/api/".insteadOf "https://api.github.com/"

# The per-run token on the worker->relay leg (LOW value: NOT a GitHub credential).
git config --global http."https://egress.<RELAY_DOMAIN>/".extraHeader "Mship-Run-Token: <token_id>.<secret>"
```

git then requests `…/gh/owner/repo.git/info/refs?service=git-receive-pack` and
`POST …/gh/owner/repo.git/git-receive-pack`; the relay strips `Mship-Run-Token` +
the inbound `Host`, attaches the minted `Authorization: token <ghs…>`, and forwards
to `github.com`.

State it plainly: **the worker holds no usable GitHub credential.** The per-run
token presented directly to GitHub is rejected; presented to the relay it unlocks
only a *run-branch* push to the *run's repos*, with a repo-scoped short-TTL App token
the worker never sees. Exfiltrating the placeholder + token yields no GitHub access
and no other-branch / other-repo write.

> **The api.github.com leg is live + enforced.** The worker opens (and lightly
> manages) its own PR through the `/api/` egress leg (operator decision B). The
> route is back on the github-app provider behind `GitHubApiEnforcer`, a
> DEFAULT-DENY REST enforcer (below). This is the API leg the auth-spine slice
> deferred, now done with a real enforcer instead of a pass-through — so the API
> path cannot sidestep the git push-to-run-branch enforcement.

## 7. Egress-proxy module boundary

The egress-proxy role is the subpackage `src/mship/core/relay/egress/`:

- `pktline.py` — `read_pkt_lines`, `parse_receive_pack_commands`, `RefUpdate` (pure git-wire).
- `request.py` — `parse_egress_request` (path-prefix host map + repo/service extraction).
- `enforce.py` — `GitSmartHttpEnforcer` (run-branch-only push), `GitHubApiEnforcer`
  (default-deny PR-only REST leg) + `classify_api_request` (its pure classifier).
- `credential.py` — `Attachment` (host-locked), `Credential`, `github_token_attachment`.
- `provider.py` — `CredentialProvider`, `GitHubAppProvider`.
- `routes.py` — `Route`, `RouteTable`, `build_default_routes`.
- `proxy.py` — `build_egress_app` (verify → route → enforce → provider → attach →
  forward; fail-closed when no provider).

The worker's session **logically terminates at `build_egress_app`**, authenticated
by the per-run token that module verifies itself (not delegated to the relay). This
self-contained, end-to-end-verifiable boundary is exactly what makes the Shape-3
relocation a deployment change rather than a rewrite.

## 8. The api.github.com leg — `GitHubApiEnforcer` (default-deny, PR-only)

The worker opens its own PR, which is a REST call (`POST /repos/{o}/{r}/pulls`).
So the `api.github.com` route is routed on the **same** github-app provider as the
git leg, behind `GitHubApiEnforcer`. Containment is two independent layers: (a) the
repo-scoped App installation token already bounds every call to the run's repos;
(b) the enforcer is DEFAULT-DENY over the REST surface — it permits an enumerated
allowlist and refuses everything else (403). New/unknown GitHub endpoints are denied
by construction: the safe failure mode is a blocked worker call, never an unexpected
mutation.

**PERMIT (and nothing else):**
- `POST  /repos/{o}/{r}/pulls` — open a PR
- `PATCH /repos/{o}/{r}/pulls/{n}` — update the run's PR (title/body/state); **not** merge
- `POST  /repos/{o}/{r}/issues/{n}/comments` — comment
- `POST  /repos/{o}/{r}/pulls/{n}/reviews`, `POST /repos/{o}/{r}/pulls/{n}/requested_reviewers` — review / request review
- `GET   /repos/{o}/{r}/...` — reads scoped to the run's repos
- `GET   /rate_limit`, `GET /user` — safe global reads

Every repo-scoped permit ALSO requires the path's `owner/repo` to be within the
run's `scope.repos` (an out-of-scope repo is refused — same containment as the git
leg).

**Explicitly DENIED (named + tested, though default-deny already covers them):**
- `PUT /repos/{o}/{r}/pulls/{n}/merge` — merging a PR. **Nothing auto-merges**; the
  fan-out is review-gated end to end (#393), so merge is denied even though the
  token technically could. Note this is a DIFFERENT path from the permitted
  `PATCH /pulls/{n}`.
- `POST /repos/{o}/{r}/merges`
- `POST|PATCH|DELETE /repos/{o}/{r}/git/refs/*` — ref mutation
- `PUT|DELETE /repos/{o}/{r}/contents/*` — content mutation

These are exactly the REST paths that could sidestep the git push-to-run-branch
enforcement, so they are called out even though default-deny refuses them anyway.
GraphQL (`/graphql`) is not routed (REST-only in v1; it would need its own enforcer).

**No new deploy surface.** The api leg rides the existing egress subdomain: the
`/api/` path prefix is already mapped in `request.py`, the worker config already sets
`url."…/api/".insteadOf "https://api.github.com/"` (Section 6), the Attachment already
host-locks the token to `[github.com, api.github.com]`, and one Caddy block + one
`tls_ask` entry already front the whole `egress.<RELAY_DOMAIN>` host. Adding the leg
was a route-table entry + enforcer, not a new route/TLS allowance. The proxy still
fails CLOSED on the api leg (App creds absent -> 503, never forward).
