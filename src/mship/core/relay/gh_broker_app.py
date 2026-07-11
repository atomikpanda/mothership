"""Broker B: the standalone relay-side service that mints short-lived,
repo-scoped GitHub App installation tokens (`mship.core.gh_app.mint_installation_token`).

Runs on the relay host (fronted by Caddy, like `enroll_app.py`) rather than on
a dev machine, so it doesn't depend on a locally-authenticated `gh` CLI — only
the GitHub App's id/private key/installation id. Shares its response contract
with Broker A (`mship serve`'s GET /gh-token in `core/serve.py`):
`{"token", "expires_at", "repositories"}`.
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException

from mship.core.gh_app import GhAppError, mint_installation_token

logger = logging.getLogger(__name__)


def _make_auth_dependency(token: str):
    """Constant-time bearer check — same pattern as `core.serve._make_auth_dependency`
    (duplicated rather than imported: serve.py pulls in the whole serve stack —
    state/spec/workitem stores — which this standalone relay service has no need of)."""
    expected = f"Bearer {token}".encode("utf-8")

    def _require_token(authorization: str | None = Header(default=None)):
        provided = (authorization or "").encode("utf-8")
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    return _require_token


def create_gh_broker_app(
    *,
    bearer_token: str,
    app_id: str | None,
    private_key: str | None,
    installation_id: str | None,
) -> FastAPI:
    """Build the gh-token broker app. `app_id`/`private_key`/`installation_id`
    are captured at build time (the CLI reads them from env — see
    `mship relay gh-broker` in `cli/relay.py`); if any is missing, the app
    still builds (so misconfiguration surfaces as a normal 500 per-request
    rather than a crash at startup), but every `/gh-token` call fails with a
    clear, secret-free error until they're set.

    The private key is only ever handed to `mint_installation_token` — never
    logged, never returned in a response."""
    app = FastAPI(
        title="mship relay gh-broker", version="0",
        dependencies=[Depends(_make_auth_dependency(bearer_token))],
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    @app.get("/gh-token")
    def get_gh_token(repos: str | None = None):
        repos_list = [r.strip() for r in repos.split(",") if r.strip()] if repos else []

        if not (app_id and private_key and installation_id):
            raise HTTPException(
                status_code=500,
                detail=(
                    "gh-broker misconfigured: missing GitHub App id/key/installation "
                    "(set MSHIP_GH_APP_ID, MSHIP_GH_APP_KEY, MSHIP_GH_APP_INSTALLATION)"
                ),
            )

        try:
            result = mint_installation_token(
                app_id=app_id,
                private_key=private_key,
                installation_id=installation_id,
                repos=repos_list,
            )
        except GhAppError as e:
            # str(e) names the requested repos, never the private key (see GhAppError
            # call sites in gh_app.py) — safe to surface verbatim.
            raise HTTPException(status_code=502, detail=str(e)) from e

        # Audit the mint: timestamp + requested repos, never the token or key.
        logger.info(
            "gh-token minted: broker=B repos=%s at=%s",
            repos_list, datetime.now(timezone.utc).isoformat(),
        )
        return result

    return app
