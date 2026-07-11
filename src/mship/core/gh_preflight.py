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
from dataclasses import dataclass
from pathlib import Path

import httpx

from mship.core.config import ConfigLoader


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
    timeout: float = 8.0,
    client: httpx.Client | None = None,
) -> PreflightResult:
    """STRICT auth check.

    Precedence mirrors `resolve_token`'s token sources (explicit > GH_TOKEN >
    GITHUB_TOKEN), but the broker leg never swallows: a non-200 response, a
    connection error, or a timeout is reported back verbatim (never returns
    "ok" on ambiguity), and there being no auth configured at all is itself a
    failure — the opposite of `resolve_token`'s "degrade to None".
    """
    token = _resolve_override_token(explicit_token)
    if token:
        return PreflightResult(
            True, "auth OK (using GH_TOKEN/--token; broker not needed)."
        )

    if not broker_url:
        return PreflightResult(
            False,
            "no GitHub auth configured (set MSHIP_GH_BROKER_URL + MSHIP_SERVE_TOKEN "
            "for the broker, or GH_TOKEN/GITHUB_TOKEN/--token).",
        )

    c, owns = (client, False) if client is not None else (httpx.Client(timeout=timeout), True)
    try:
        try:
            params = {"repos": ",".join(repos)} if repos else None
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

        return PreflightResult(True, f"auth OK — broker covers: {', '.join(repos)}")
    finally:
        if owns:
            c.close()
