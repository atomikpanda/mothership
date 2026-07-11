"""Mint short-lived, repo-scoped GitHub App installation tokens (Broker B).

The App private key signs a short-lived App JWT (RS256), which is exchanged
for an installation access token scoped to only the requested repos. The
private key never leaves this process and is never logged or included in an
error message.
"""
from __future__ import annotations

import time

import httpx
import jwt

_API = "https://api.github.com"


class GhAppError(Exception):
    pass


def _app_jwt(app_id: str, private_key: str, now: int | None = None) -> str:
    """Sign a short-lived (10 min) App JWT identifying the GitHub App as `iss`."""
    now = now if now is not None else int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
        private_key,
        algorithm="RS256",
    )


def mint_installation_token(
    *,
    app_id: str,
    private_key: str,
    installation_id: str,
    repos: list[str],
    now: int | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """Return `{"token", "expires_at", "repositories"}` scoped to `repos`
    (short repo names, e.g. ["mothership", "ground-control"]).

    Raises GhAppError (message naming the requested repos) if the App
    installation can't cover the request. Never logs or returns the private
    key.

    `repos` must be non-empty: an empty/omitted `repositories` list in the
    GitHub API request mints a token scoped to the ENTIRE App installation,
    which this broker must never do by accident.
    """
    if not repos:
        raise GhAppError(
            "gh-app: refusing to mint an unscoped token — repos must be non-empty"
        )

    token_jwt = _app_jwt(app_id, private_key, now)
    body = {"repositories": list(repos)}
    c, owns = (client, False) if client is not None else (httpx.Client(timeout=15), True)
    try:
        try:
            resp = c.post(
                f"{_API}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {token_jwt}",
                    "Accept": "application/vnd.github+json",
                },
                json=body,
            )
        except httpx.HTTPError as e:
            raise GhAppError(f"gh-app: request failed: {e}") from e

        if resp.status_code == 201:
            data = resp.json()
            token = data.get("token")
            if not isinstance(token, str) or not token:
                raise GhAppError(
                    f"gh-app: installation-token response had no token (repos {list(repos)})"
                )
            return {
                "token": token,
                "expires_at": data.get("expires_at"),
                "repositories": list(repos),
            }

        raise GhAppError(
            f"gh-app: installation-token mint failed ({resp.status_code}) for repos "
            f"{list(repos)} — check the App is installed on each: {resp.text[:300]}"
        )
    finally:
        if owns:
            c.close()
