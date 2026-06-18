"""Configure git/gh auth from an environment token for cloud sessions (MOS-187).

The token is passed to git only via a subprocess env var read by a github.com-
scoped credential helper — never written to disk, never placed in argv.
"""
from __future__ import annotations

import os

import httpx

# Credential helper: on a `get` request, emit username/password reading the
# token from the env var git inherits. Single-quote-safe (uses double quotes
# internally) so callers can shlex.quote the whole `-c` value.
_CRED_HELPER = (
    '!f() { test "$1" = get || exit 0; '
    '[ -n "$MSHIP_GH_TOKEN" ] || { echo "MSHIP_GH_TOKEN unset" >&2; exit 1; }; '
    'printf "username=x-access-token\\npassword=%s\\n" "$MSHIP_GH_TOKEN"; }; f'
)
_TOKEN_ENV_VAR = "MSHIP_GH_TOKEN"


def resolve_token(explicit: str | None) -> str | None:
    """Token precedence: explicit (--token) > GH_TOKEN > GITHUB_TOKEN. Blank → None."""
    for candidate in (explicit, os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN")):
        if candidate and candidate.strip():
            return candidate.strip()
    return None


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
    """Open a PR via the GitHub REST API (no gh dependency). Returns html_url."""
    c, owns = _make_client(client)
    try:
        resp = c.post(
            f"{_API}/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
            headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
        )
    finally:
        if owns:
            c.close()
    if resp.status_code >= 300:
        raise RuntimeError(
            f"GitHub PR creation failed ({resp.status_code}): {resp.text[:300]}"
        )
    url = resp.json().get("html_url")
    if not isinstance(url, str) or not url:
        raise RuntimeError("GitHub PR created but response had no html_url")
    return url


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
