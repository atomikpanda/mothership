from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from mship.core.gh_app import GhAppError, mint_installation_token, resolve_installation
from mship.core.relay.grants import Grant
from mship.core.relay.egress.credential import Credential, github_token_attachment
from mship.core.relay.egress.request import EgressRequest


class ProviderError(Exception):
    """The provider could not resolve a credential for this request."""


@runtime_checkable
class CredentialProvider(Protocol):
    def resolve(self, identity: str, grant: Grant, request: EgressRequest) -> Credential: ...


class GitHubAppProvider:
    """Resolve a GitHub App installation token scoped to the grant's repos.

    A future StaticSecretProvider / GitLabProvider implements the same
    `resolve(identity, grant, request)` and returns a Credential — the proxy
    core does not change."""

    def __init__(self, *, app_id: str, private_key: str, client: httpx.Client | None = None):
        self._app_id = app_id
        self._private_key = private_key
        self._client = client

    def resolve(self, identity: str, grant: Grant, request: EgressRequest) -> Credential:
        repos = list(grant.scope.repos)
        if not repos:
            raise ProviderError("refusing to mint an unscoped token — grant has no repos")
        if request.repo is not None and request.repo not in repos:
            raise ProviderError(f"repo {request.repo!r} is outside the grant {repos}")

        owners = {r.split("/", 1)[0] for r in repos}
        if len(owners) > 1:
            raise ProviderError(f"grant repos span multiple owners {sorted(owners)}")
        owner = owners.pop()
        short_names = [r.split("/", 1)[1] for r in repos]
        try:
            installation_id = resolve_installation(
                app_id=self._app_id, private_key=self._private_key,
                owner=owner, repo=short_names[0], client=self._client,
            )
            minted = mint_installation_token(
                app_id=self._app_id, private_key=self._private_key,
                installation_id=installation_id, repos=short_names, client=self._client,
            )
        except GhAppError as e:
            raise ProviderError(str(e)) from e
        return Credential(
            value=minted["token"],
            expires_at=minted.get("expires_at"),
            attach=github_token_attachment(),
        )
