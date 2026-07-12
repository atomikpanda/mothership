# GitHub Auth for Cloud Agent Sessions

Cloud/CI agent containers need to push commits and open PRs without a locally
logged-in `gh` CLI or a hand-copied personal access token (PAT) baked into the
routine, prompt, or logs. mship solves this with one token endpoint: the cloud
container asks `mship serve` for a short-lived, repo-scoped token at the moment
it needs one, instead of holding a long-lived credential itself.

There is a single broker — `mship serve`'s `GET /gh-token` — with two backends,
chosen automatically by whether a GitHub App is configured on the serve host:

- **App-backed (unattended + multi-account).** If `MSHIP_GH_APP_ID` +
  `MSHIP_GH_APP_KEY` are set on the serve host, serve mints short-lived GitHub
  App installation tokens. It resolves *which* installation owns each requested
  repo on the fly, so one App installed across several accounts/orgs covers all
  of them — no installation id to configure. Works even when every laptop is
  asleep.
- **`gh auth token` fallback (zero setup, daytime).** If no App is configured,
  serve proxies the serve host's own `gh auth token` (so `gh auth login` on that
  machine is the only requirement). Single-identity, and only while that host is
  awake and logged in.

Both return the same contract — bearer-auth'd
`GET /gh-token?repos=<comma-separated>` returning
`{"token", "expires_at", "repositories"}` — so client code
(`mship.core.gh_auth.resolve_token`) doesn't care which backend answers.

---

## 1. Fresh cloud container recipe

Set two environment variables once per environment (container image, CI secret,
devcontainer config, …) — never a PAT in the routine, the prompt, or anywhere
that ends up in logs:

```bash
export MSHIP_GH_BROKER_URL=https://<workspace-slug>-<hex>.relay.example.com  # the mship serve URL (through the relay)
export MSHIP_SERVE_TOKEN=<the serve bearer token>
```

That's it. `mship bootstrap` and `mship finish` call `resolve_token` internally
and pull a fresh token from serve automatically — no other config needed, and
nothing about the token touches disk or argv. The client sends the workspace's
`owner/repo` names so serve can resolve the right installation.

Token precedence (highest to lowest): `--token` flag > `GH_TOKEN` env >
`GITHUB_TOKEN` env > broker pull. So if you ever need to override the broker
(e.g. a one-off manual PAT for local debugging), set `GH_TOKEN` or pass
`--token` — both still work exactly as before and the broker is skipped
entirely.

---

## 2. GitHub App setup (for unattended/overnight and multi-account runs)

The App backend mints installation tokens without depending on any dev machine
being awake, and one App can span every account/org you own. One-time setup on
GitHub, then the App creds on the serve host.

**Create the App** (GitHub → Settings → Developer settings → GitHub Apps → New
GitHub App):

- Permissions: **Contents** — Read and write, **Pull requests** — Read and
  write. (No other permissions needed.)
- Where can this app be installed: **Any account** — this is what lets one App
  cover repos across several accounts/orgs.

**Install it** on every account/org whose repos your workspaces touch — install
on each one, not just a single repo. `mship gh preflight` (below) fails fast
naming any repo the App isn't installed on, so it's worth getting this right up
front.

**Download the private key** (.pem) from the App's settings page — GitHub only
lets you download it once at generation time.

**Configure the serve host.** Put the key on the serve host (not in git — keep
it under a gitignored path such as `docker/relay/keys/` or a gitignored
`.env`), then set:

```bash
MSHIP_GH_APP_ID=<the App's id>
MSHIP_GH_APP_KEY=/path/to/the-app.private-key.pem   # file PATH, not the PEM text
```

There is **no installation id to set** — serve resolves the installation per
repo from the App key. (If you still have `MSHIP_GH_APP_INSTALLATION` set from
an older setup, serve ignores it and logs a one-line warning; you can remove
it.)

**Run serve** as usual — the App backend activates automatically because the
creds are present:

```bash
mship serve --relay
```

Point cloud containers at the serve URL (`MSHIP_GH_BROKER_URL`, §1). No separate
broker process and no separate Caddy route: the `/gh-token` endpoint rides on
the same serve tunnel your workspace already exposes.

### Multi-account, single-account-per-workspace

A GitHub App installation token is scoped to one installation (one account/org).
Each mship **workspace** is expected to stay within a single account, so every
`/gh-token` request's repos share one installation and get one token. Different
workspaces under different accounts each auto-resolve to their own installation
from the same App and the same serve — no per-workspace auth difference. A
request whose repos span more than one account is rejected with a clear error
(a workspace must be single-account).

### Identity is never silently swapped

If the App **is** configured but isn't installed on a requested repo's owner,
`/gh-token` returns a hard error naming the repo ("install the App on
`{owner}`") — it does **not** fall back to `gh auth token`. That keeps every
push unambiguously attributable to the App, and tells you your setup is
incomplete instead of quietly pushing as a different identity.

---

## 3. Zero-setup daytime option (`gh auth token`)

If a workspace only ever runs while your own machine is awake, you don't need a
GitHub App at all. Leave `MSHIP_GH_APP_ID`/`MSHIP_GH_APP_KEY` unset and
`mship serve`'s `/gh-token` proxies the serve host's own `gh auth token` (so
`gh auth login` on that machine is the only requirement). Point cloud containers
at the serve URL exactly as in §1:

```bash
export MSHIP_GH_BROKER_URL=https://<workspace-slug>-<hex>.relay.example.com
export MSHIP_SERVE_TOKEN=<same token mship serve --relay prints/uses>
```

This is the lower-effort option for interactive/daytime cloud sessions; it just
isn't available for truly unattended runs where nothing is logged in locally,
and it's single-identity (no multi-account).

---

## 4. Verify: `mship gh preflight`

Before scheduling an unattended run, confirm auth actually covers every repo the
workspace needs — `mship gh preflight` is strict by design (unlike
`resolve_token`, which quietly degrades to "no token" so interactive
bootstrap/finish can still proceed): it fails fast and names the specific repo
the App isn't installed on, rather than letting a run burn AI tokens on code
that then can't be pushed.

```bash
mship gh preflight
# ✓ auth OK — broker covers: acme/repo-a, acme/repo-b, acme/repo-c
# or, on failure:
# ✗ broker auth check failed (502): App is not installed on acme/repo-b
#   -> install the App on the named owner above, then retry.
```

Only schedule unattended/overnight runs once this is green. It checks
`GH_TOKEN`/`GITHUB_TOKEN`/`--token` first (same precedence as `resolve_token`)
and only falls through to the serve broker if none of those are set — so it
works whether you're using the App backend, the `gh auth token` fallback, or a
plain PAT override.
