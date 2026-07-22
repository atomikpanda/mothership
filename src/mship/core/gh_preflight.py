"""Fail-fast GitHub auth check for unattended runs (`mship gh preflight`).

This is the deliberate OPPOSITE of `gh_auth.resolve_token`: `resolve_token`
swallows every broker failure and degrades to "no token" so bootstrap/finish
can proceed regardless. This check is STRICT — any broker error, or the
absence of any auth at all, is a loud, non-zero-exit failure. An unattended
overnight run is meant to invoke this FIRST and abort before spending AI
tokens on code it then can't push.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from mship.core.clone_url import resolve_clone_url
from mship.core.config import ConfigLoader

_API = "https://api.github.com"

# Same shape of parse as `pr.py`'s `_parse_github_slug` (turning a github.com
# remote URL into an `owner/repo` slug) — duplicated locally rather than
# imported so this module doesn't reach into another module's private name;
# both exist to solve the same narrow problem of reading a git remote.
_GITHUB_SLUG_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    message: str


def repo_set_from_config(config_path: Path, repos: list[str] | None = None) -> list[str]:
    """Resolve the repo set to preflight: an explicit list if given, else every
    non-`git_root` repo in the workspace config — the same set bootstrap/finish
    fall back to for their broker-pull (git_root repos are subdirectories of
    their parent's checkout, not independently-installed GitHub repos, so a
    broker mint request must never name one).

    Loaded with `require_paths=False`: this is an auth-only check that must
    work even before a workspace's repos are cloned (e.g. run right after
    `mship bootstrap` on a fresh cloud checkout).
    """
    config = ConfigLoader.load(Path(config_path), require_paths=False)
    if repos:
        return list(repos)
    return [n for n, r in config.repos.items() if r.git_root is None]


def _parse_github_owner_repo(url: str) -> str | None:
    m = _GITHUB_SLUG_RE.search(url.strip())
    if not m:
        return None
    return f"{m.group('owner')}/{m.group('repo')}"


def repo_owner_names_from_config(config_path: Path, repos: list[str]) -> dict[str, str]:
    """Resolve each of `repos` (short config names) to a GitHub `owner/name`
    slug, via the exact same clone-URL resolution bootstrap uses
    (`resolve_clone_url`: per-repo `url` override, else workspace
    `default_remote` + repo name) — so the override-token verify path checks
    coverage of the workspace's real GitHub repos, not just its config keys.

    Returns `{repo_name: "owner/name"}`. A repo is OMITTED when it has no
    resolvable url at all (no `url`/`default_remote`), or when it resolves to
    a non-github.com remote (this module only knows how to verify token
    coverage against the GitHub REST API) — `run_preflight` treats a missing
    entry as "can't verify" and fails rather than silently skipping it.
    """
    config = ConfigLoader.load(Path(config_path), require_paths=False)
    resolved: dict[str, str] = {}
    for name in repos:
        repo = config.repos.get(name)
        if repo is None:
            continue
        url = resolve_clone_url(name, repo, config.default_remote)
        if url is None:
            continue
        slug = _parse_github_owner_repo(url)
        if slug is not None:
            resolved[name] = slug
    return resolved


def verify_repos_pushable(
    *,
    base_url: str,
    auth_headers: dict[str, str],
    repo_owner_names: list[str],
    timeout: float = 8.0,
    client: httpx.Client | None = None,
) -> str | None:
    """STRICT check that the caller's auth can push to every `owner/name`, via
    `GET {base_url}/repos/{owner}/{name}` carrying `auth_headers`. ONE
    verification, two transports:
      - override-token: base_url=api.github.com, {Authorization: Bearer <tok>}
      - relay-attach:   base_url=<relay>/api,    {Mship-Run-Token: <run tok>}

    Returns None when every repo responds 200 with `permissions.push` true.
    Returns a clear, non-None error string on the first problem:
      401 -> "token is invalid or expired"; 403/404 or 200-without-push ->
      "token cannot push to {owner/name}"; any other non-200, a non-JSON body,
      a connection error, or a timeout -> a clear error naming the repo (never
      silently passes on ambiguity)."""
    c, owns = (client, False) if client is not None else (httpx.Client(timeout=timeout), True)
    try:
        for owner_name in repo_owner_names:
            try:
                resp = c.get(f"{base_url}/repos/{owner_name}", headers=auth_headers)
            except httpx.HTTPError as e:
                return f"token check request failed for {owner_name}: {e}"

            if resp.status_code == 401:
                return "token is invalid or expired"
            if resp.status_code in (403, 404):
                return f"token cannot push to {owner_name}"
            if resp.status_code != 200:
                return (
                    f"token check for {owner_name} failed "
                    f"({resp.status_code}): {resp.text[:200]}"
                )

            try:
                body = resp.json()
            except ValueError:
                return f"token check for {owner_name} returned a non-JSON response"

            permissions = body.get("permissions") if isinstance(body, dict) else None
            can_push = isinstance(permissions, dict) and bool(permissions.get("push"))
            if not can_push:
                return f"token cannot push to {owner_name}"
        return None
    finally:
        if owns:
            c.close()


def verify_token_covers_repos(
    *,
    token: str,
    repo_owner_names: list[str],
    timeout: float = 8.0,
    client: httpx.Client | None = None,
) -> str | None:
    """STRICT check that `token` can push to every `owner/name`, via
    `GET /repos/{owner}/{name}`. The override-token transport of
    `verify_repos_pushable` (GitHub REST + a bearer); the verification lives
    once in that helper.
    """
    return verify_repos_pushable(
        base_url=_API,
        auth_headers={"Authorization": f"Bearer {token}"},
        repo_owner_names=repo_owner_names,
        timeout=timeout,
        client=client,
    )


def _resolve_override_token(explicit: str | None) -> str | None:
    for candidate in (explicit, os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN")):
        if candidate and candidate.strip():
            return candidate.strip()
    return None


def _broker_error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            return body["detail"]
    except ValueError:
        pass
    return resp.text[:300]


def run_preflight(
    *,
    explicit_token: str | None,
    broker_url: str | None,
    broker_bearer: str | None,
    repos: list[str],
    repo_owner_names: dict[str, str] | None = None,
    relay_url: str | None = None,
    run_token: str | None = None,
    timeout: float = 8.0,
    client: httpx.Client | None = None,
) -> PreflightResult:
    """STRICT auth check.

    Precedence mirrors `resolve_token`'s token sources (explicit > GH_TOKEN >
    GITHUB_TOKEN), but the broker leg never swallows: a non-200 response, a
    connection error, or a timeout is reported back verbatim (never returns
    "ok" on ambiguity), and there being no auth configured at all is itself a
    failure — the opposite of `resolve_token`'s "degrade to None".

    The override-token branch used to return "ok" the moment any token was
    found, without checking it could actually reach the workspace's repos
    (an expired or under-scoped token would then only fail later, at
    bootstrap/finish's clone/push/PR step). It now verifies coverage via
    `verify_token_covers_repos`, using `repo_owner_names` — a
    `{repo_name: "owner/name"}` map built by the caller (see
    `repo_owner_names_from_config`) the same way bootstrap resolves each
    repo's clone URL. A repo in `repos` with no entry in `repo_owner_names`
    (couldn't be resolved to a github.com owner/repo at all) fails preflight
    rather than being silently skipped.
    """
    # Relay-attach mode (distinct third mode): probe each repo through the
    # relay's /api leg carrying the run-token header. Returns within this
    # branch — never falls through to the override-token / broker branches.
    if relay_url and run_token:
        from mship.core.relay.contract import API_PREFIX, RUN_TOKEN_HEADER
        owner_names = repo_owner_names or {}
        missing = [n for n in repos if n not in owner_names]
        if missing:
            return PreflightResult(
                False,
                f"cannot verify the relay covers {', '.join(missing)}: no "
                "resolvable GitHub owner (set `url` on the repo or "
                "`default_remote` on the workspace)",
            )
        slugs = [owner_names[n] for n in repos]
        base_url = f"{relay_url.rstrip('/')}{API_PREFIX}".rstrip("/")
        error = verify_repos_pushable(
            base_url=base_url,
            auth_headers={RUN_TOKEN_HEADER: run_token},
            repo_owner_names=slugs, timeout=timeout, client=client,
        )
        if error:
            return PreflightResult(
                False, f"{error} — relay auth can't push through {relay_url}"
            )
        return PreflightResult(True, f"auth OK — relay covers: {', '.join(slugs)}")

    token = _resolve_override_token(explicit_token)
    if token:
        owner_names = repo_owner_names or {}
        missing = [n for n in repos if n not in owner_names]
        if missing:
            return PreflightResult(
                False,
                f"cannot verify --token/GH_TOKEN covers {', '.join(missing)}: "
                "no resolvable GitHub owner (set `url` on the repo or "
                "`default_remote` on the workspace) — the --token/GH_TOKEN "
                "doesn't cover the workspace repos",
            )
        slugs = [owner_names[n] for n in repos]
        error = verify_token_covers_repos(
            token=token, repo_owner_names=slugs, timeout=timeout, client=client,
        )
        if error:
            return PreflightResult(
                False,
                f"{error} — the --token/GH_TOKEN doesn't cover the workspace repos",
            )
        return PreflightResult(
            True, f"auth OK — token covers: {', '.join(slugs)}"
        )

    if not broker_url:
        return PreflightResult(
            False,
            "no GitHub auth configured (set MSHIP_GH_BROKER_URL + MSHIP_SERVE_TOKEN "
            "for the broker, or GH_TOKEN/GITHUB_TOKEN/--token).",
        )

    # The folded App-backed serve resolves a GitHub App installation per
    # owner/repo, so send `owner/repo` slugs (from the caller's map) rather
    # than the short config names — falling back to the short name for any
    # repo that couldn't be resolved to a github.com owner.
    owner_repos = (
        [repo_owner_names.get(r, r) for r in repos] if repo_owner_names else repos
    )
    c, owns = (client, False) if client is not None else (httpx.Client(timeout=timeout), True)
    try:
        try:
            params = {"repos": ",".join(owner_repos)} if owner_repos else None
            headers = {"Authorization": f"Bearer {broker_bearer}"} if broker_bearer else {}
            resp = c.get(f"{broker_url}/gh-token", params=params, headers=headers)
        except httpx.HTTPError as e:
            return PreflightResult(
                False, f"broker unreachable at {broker_url}: {e}"
            )

        if resp.status_code != 200:
            detail = _broker_error_detail(resp)
            return PreflightResult(
                False,
                f"broker auth check failed ({resp.status_code}): {detail}\n"
                "  -> install/grant the GitHub App on the named repo(s) above, then retry.",
            )

        return PreflightResult(True, f"auth OK — broker covers: {', '.join(owner_repos)}")
    finally:
        if owns:
            c.close()
