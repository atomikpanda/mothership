"""Configure git/gh auth from an environment token for cloud sessions (MOS-187).

The token is passed to git only via a subprocess env var read by a github.com-
scoped credential helper — never written to disk, never placed in argv.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Credential helper: on a `get` request, emit username/password reading the
# token from the env var git inherits. Single-quote-safe (uses double quotes
# internally) so callers can shlex.quote the whole `-c` value.
_CRED_HELPER = (
    '!f() { test "$1" = get || exit 0; '
    '[ -n "$MSHIP_GH_TOKEN" ] || { echo "MSHIP_GH_TOKEN unset" >&2; exit 1; }; '
    'printf "username=x-access-token\\npassword=%s\\n" "$MSHIP_GH_TOKEN"; }; f'
)
_TOKEN_ENV_VAR = "MSHIP_GH_TOKEN"


def broker_config_from_env() -> tuple[str | None, str | None]:
    """Read (broker_url, broker_bearer) for the runtime gh-token broker — the
    `mship serve` `GET /gh-token?repos=...` endpoint (App-backed when an App is
    configured on the serve host, else proxying `gh auth token`) — from the
    environment, so every call site reads the same two envs identically:

    - MSHIP_GH_BROKER_URL: the broker's base URL (no trailing `/gh-token`).
    - MSHIP_SERVE_TOKEN: the bearer token, shared with the broker's own
      `_make_auth_dependency` bearer check.
    """
    return os.environ.get("MSHIP_GH_BROKER_URL"), os.environ.get("MSHIP_SERVE_TOKEN")


def resolve_token(
    explicit: str | None,
    *,
    broker_url: str | None = None,
    broker_bearer: str | None = None,
    repos: list[str] | None = None,
    timeout: float = 8.0,
    client: httpx.Client | None = None,
) -> str | None:
    """Token precedence: explicit (--token) > GH_TOKEN > GITHUB_TOKEN > broker
    pull. Blank → None.

    The broker pull is the lowest-precedence, last-resort source: it only runs
    when none of the first three sources yield a token AND `broker_url` is
    given (existing zero-arg-broker callers are unaffected — they keep today's
    behavior exactly). It does a `GET {broker_url}/gh-token` (with `?repos=`
    scoping the request when `repos` is given) carrying `Authorization: Bearer
    {broker_bearer}`, and returns the `"token"` field of the JSON body.

    Resilient by design: any failure — non-200 status, timeout, connection
    error, or a malformed/missing token in the body — is logged as a warning
    and yields None. This function never raises for broker failures, so a
    broker outage degrades to "no token" rather than crashing the caller.
    """
    for candidate in (explicit, os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN")):
        if candidate and candidate.strip():
            return candidate.strip()

    if not broker_url:
        return None

    c, owns = (client, False) if client is not None else (httpx.Client(timeout=timeout), True)
    try:
        params = {"repos": ",".join(repos)} if repos else None
        headers = {"Authorization": f"Bearer {broker_bearer}"} if broker_bearer else {}
        resp = c.get(f"{broker_url}/gh-token", params=params, headers=headers)
        if resp.status_code != 200:
            logger.warning(
                "gh-token broker pull failed (%s): %s",
                resp.status_code, resp.text[:200],
            )
            return None
        token = resp.json().get("token")
        if not isinstance(token, str) or not token.strip():
            logger.warning("gh-token broker response had no usable token")
            return None
        return token.strip()
    except httpx.HTTPError as e:
        logger.warning("gh-token broker pull request failed: %s", e)
        return None
    except Exception as e:  # defensive: malformed JSON etc. must never raise
        logger.warning("gh-token broker pull failed: %s", e)
        return None
    finally:
        if owns:
            c.close()


def git_cred_args(token: str) -> tuple[list[str], dict[str, str]]:
    """Return (`-c` args for a github.com-scoped credential helper, env carrying
    the token). Splice the args into a `git` invocation and pass the env to it.
    The token appears only in the env, never in the args."""
    keyval = f"credential.https://github.com.helper={_CRED_HELPER}"
    return ["-c", keyval], {_TOKEN_ENV_VAR: token}


_API = "https://api.github.com"
_API_HEADERS = {"Accept": "application/vnd.github+json"}


def _make_client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return httpx.Client(timeout=30.0), True


def create_pr_via_httpx(
    token: str, owner: str, repo: str, *, head: str, base: str,
    title: str, body: str, client: httpx.Client | None = None,
) -> str:
    """Open a PR via the GitHub REST API (no gh dependency). Returns html_url.

    Idempotent: if a PR already exists for the branch (GitHub 422), recover and
    return the existing PR's URL instead of raising — so retrying `mship finish`
    after a partial cloud run succeeds (mirrors the gh path)."""
    c, owns = _make_client(client)
    try:
        resp = c.post(
            f"{_API}/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
            headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 422 and "already exist" in resp.text.lower():
            existing = _find_open_pr_url(c, token, owner, repo, head)
            if existing:
                return existing
        if resp.status_code >= 300:
            raise RuntimeError(
                f"GitHub PR creation failed ({resp.status_code}): {resp.text[:300]}"
            )
        url = resp.json().get("html_url")
        if not isinstance(url, str) or not url:
            raise RuntimeError("GitHub PR created but response had no html_url")
        return url
    except httpx.HTTPError as e:
        # Network/connectivity failures (ConnectError, TimeoutException, …) are
        # httpx.HTTPError, not RuntimeError — re-raise so the finish caller's
        # `except RuntimeError` handles them cleanly instead of crashing.
        raise RuntimeError(f"GitHub PR creation request failed: {e}") from e
    finally:
        if owns:
            c.close()


def _find_open_pr_url(
    client: httpx.Client, token: str, owner: str, repo: str, head: str,
) -> str | None:
    """Return the html_url of the open PR for `head` (same-repo branch), or None."""
    resp = client.get(
        f"{_API}/repos/{owner}/{repo}/pulls",
        params={"head": f"{owner}:{head}", "state": "open"},
        headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
    )
    if resp.status_code >= 300:
        return None
    items = resp.json()
    if isinstance(items, list) and items:
        url = items[0].get("html_url")
        if isinstance(url, str) and url:
            return url
    return None


def get_default_branch_via_httpx(
    token: str, owner: str, repo: str, *, client: httpx.Client | None = None,
) -> str:
    """Fetch the repo's default branch (used when no base is given)."""
    c, owns = _make_client(client)
    try:
        resp = c.get(
            f"{_API}/repos/{owner}/{repo}",
            headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not fetch default branch: {e}") from e
    finally:
        if owns:
            c.close()
    if resp.status_code >= 300:
        raise RuntimeError(
            f"Could not fetch default branch ({resp.status_code}): {resp.text[:200]}"
        )
    branch = resp.json().get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise RuntimeError(
            f"GitHub repo response for {owner}/{repo} had no default_branch"
        )
    return branch


#: Auth models in precedence order, most specific first. Mirrors EXACTLY the
#: token precedence documented on `resolve_token` (explicit > GH_TOKEN >
#: GITHUB_TOKEN > broker), with relay-attach ahead of them all (a worker routed
#: through the relay egress never holds a token of its own).
#:
#: A configured GitHub App is deliberately NOT a model here. App credentials are
#: consumed only by serve's `GET /gh-token` to mint tokens for CALLERS (Broker
#: B); the host's own git operations still resolve through `resolve_token`. So an
#: App-backed serve that also has GH_TOKEN set uses GH_TOKEN, and reporting
#: "app" would name a different model than the one bootstrap/finish actually
#: use. Callers that care about the serve capability should report it separately
#: (see `topology._gh_auth_edge`'s `serves_app_backed_tokens`).
#:
#: NOTE: `core.gh_preflight.run_preflight` branches on the same precedence for
#: its STRICT, network-verifying check. That duplication is pre-existing and
#: deliberately left alone here — this function is the *reporting* owner and must
#: never raise or touch the network.
GH_AUTH_MODELS = ("relay_attach", "env_token", "broker", "none")


def classify_gh_auth(
    *,
    relay_url: str | None,
    run_token: str | None,
    explicit_token: str | None,
    broker_url: str | None,
) -> str:
    """Which GitHub auth model is in effect, by name (one of GH_AUTH_MODELS).

    Pure: no network, no environment reads (the caller supplies the values so
    the same function serves a report, a test, and a future dry-run). Blank
    strings count as absent.
    """
    def present(value: str | None) -> bool:
        return bool(value and value.strip())

    if present(relay_url) and present(run_token):
        return "relay_attach"
    if present(explicit_token):
        return "env_token"
    if present(broker_url):
        return "broker"
    return "none"
