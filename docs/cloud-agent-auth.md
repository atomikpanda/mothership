# GitHub Auth for Cloud Agent Sessions

Cloud/CI agent containers need to push commits and open PRs without a locally
logged-in `gh` CLI or a hand-copied personal access token (PAT) baked into the
routine, prompt, or logs. mship solves this with a small token broker: the
cloud container asks a broker for a short-lived, repo-scoped token at the
moment it needs one, instead of holding a long-lived credential itself.

There are two brokers, and you only need one of them running for a given
environment:

- **Broker A** — `mship serve`'s `GET /gh-token`. Zero setup: it proxies the
  serve host's own `gh auth token`. Only works while that host is awake and
  logged in via `gh auth login`.
- **Broker B** — the standalone `mship relay gh-broker` service. Mints tokens
  itself from a GitHub App installation, so it works even when every laptop
  is asleep. This is the one to set up for unattended/overnight cloud runs.

Both brokers expose the same contract — bearer-auth'd
`GET /gh-token?repos=<comma-separated>` returning
`{"token", "expires_at", "repositories"}` — so client code
(`mship.core.gh_auth.resolve_token`) doesn't care which one answers.

---

## 1. Fresh cloud container recipe

Set two environment variables once per environment (container image, CI
secret, devcontainer config, …) — never a PAT in the routine, the prompt, or
anywhere that ends up in logs:

```bash
export MSHIP_GH_BROKER_URL=https://gh.relay.example.com   # or the serve-host URL for Broker A
export MSHIP_SERVE_TOKEN=<the broker's bearer token>
```

That's it. `mship bootstrap` and `mship finish` call `resolve_token`
internally and pull a fresh token from the broker automatically — no other
config needed, and nothing about the token touches disk or argv.

Token precedence (highest to lowest): `--token` flag > `GH_TOKEN` env >
`GITHUB_TOKEN` env > broker pull. So if you ever need to override the broker
(e.g. a one-off manual PAT for local debugging), set `GH_TOKEN` or pass
`--token` — both still work exactly as before and the broker is skipped
entirely.

---

## 2. GitHub App setup (Broker B — for unattended/overnight runs)

Broker B mints installation tokens from a GitHub App, so it doesn't depend on
any dev machine being awake. One-time setup on GitHub, then one process on
the relay host.

**Create the App** (GitHub → Settings → Developer settings → GitHub Apps →
New GitHub App):

- Permissions: **Contents** — Read and write, **Pull requests** — Read and
  write. (No other permissions needed.)
- Where can this app be installed: your choice (only your account, if this
  broker is for your own workspaces).

**Install it** on every repo the workspace touches — all of them, not just
one. `mship gh preflight` (below) fails fast naming any repo the App isn't
installed on, so it's worth getting this list right up front.

**Download the private key** (.pem) from the App's settings page — GitHub
only lets you download it once at generation time.

**Configure the relay host.** Put the key on the relay (not in git — keep it
under a gitignored path such as `docker/relay/keys/` or a `docker/relay/.env`
file that's already gitignored), then set:

```bash
MSHIP_GH_APP_ID=<the App's id>
MSHIP_GH_APP_KEY=/path/to/the-app.private-key.pem   # file PATH, not the PEM text
MSHIP_GH_APP_INSTALLATION=<the installation id>       # from the app's installation URL
```

**Run the broker:**

```bash
mship relay gh-broker \
  --app-id "$MSHIP_GH_APP_ID" \
  --app-key "$MSHIP_GH_APP_KEY" \
  --installation-id "$MSHIP_GH_APP_INSTALLATION"
# binds 127.0.0.1:47181 by default; Caddy fronts it publicly (see below).
```

The bearer token it requires is the same one `mship serve --relay` uses
(`ensure_serve_token` — persisted under `--token-dir`, or `MSHIP_SERVE_TOKEN`
if already set).

**Add the Caddy ingress.** The relay's `docker/relay/Caddyfile` has a
`gh.{$RELAY_DOMAIN}` route (alongside the existing `enroll.{$RELAY_DOMAIN}`
route) that reverse-proxies to `127.0.0.1:47181`, allow-listing only
`GET /gh-token` — every other method/path gets a 404 at the edge, before it
ever reaches the broker. The on-demand TLS `ask` check
(`mship.core.relay.tls_ask.tls_ask_allowed`) approves `gh.<relay-domain>`
the same way it already approves `enroll.<relay-domain>`, so Caddy issues it
a cert automatically the first time it's hit — no separate cert step. Point
cloud containers at `MSHIP_GH_BROKER_URL=https://gh.<relay-domain>`.

---

## 3. Broker A (serve host) — zero setup

If the workspace only ever runs while your own machine is awake, you don't
need a GitHub App at all. `mship serve` already exposes `GET /gh-token`,
which proxies the serve host's own `gh auth token` (so `gh auth login` on
that machine is the only requirement). Point cloud containers at the serve
host's URL — through the relay tunnel if the host isn't otherwise reachable:

```bash
export MSHIP_GH_BROKER_URL=https://<workspace-slug>-<hex>.relay.example.com
export MSHIP_SERVE_TOKEN=<same token mship serve --relay prints/uses>
```

This is the lower-effort option for interactive/daytime cloud sessions; it
just isn't available for truly unattended runs where nothing is logged in
locally.

---

## 4. Verify: `mship gh preflight`

Before scheduling an unattended run, confirm auth actually covers every repo
the workspace needs — `mship gh preflight` is strict by design (unlike
`resolve_token`, which quietly degrades to "no token" so interactive
bootstrap/finish can still proceed): it fails fast and names the specific
repo the App isn't installed on, rather than letting a run burn AI tokens on
code that then can't be pushed.

```bash
mship gh preflight
# ✓ auth OK — broker covers: repo-a, repo-b, repo-c
# or, on failure:
# ✗ broker auth check failed (404): ... -> install/grant the GitHub App on the named repo(s) above, then retry.
```

Only schedule unattended/overnight runs once this is green. It checks
`GH_TOKEN`/`GITHUB_TOKEN`/`--token` first (same precedence as
`resolve_token`) and only falls through to the broker if none of those are
set — so it works whether you're using Broker A, Broker B, or a plain PAT
override.
