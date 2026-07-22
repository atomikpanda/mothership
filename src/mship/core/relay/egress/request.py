from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs

# Path prefix -> upstream host. Adding a host = one entry here + one route
# (routes.py) + one tls_ask/Caddy allowance. No github.com special-case in code.
_PREFIX_HOST = {"/gh/": "github.com", "/api/": "api.github.com"}


class UnmappablePathError(Exception):
    """Incoming path did not start with a known egress prefix (/gh/, /api/)."""


@dataclass(frozen=True)
class EgressRequest:
    method: str
    upstream_host: str
    upstream_path: str
    query: str
    headers: dict
    body: bytes
    repo: str | None            # owner/repo for git hosts; None for the API host
    service: str | None         # git-upload-pack | git-receive-pack | None
    is_receive_pack_post: bool


def _extract_repo(upstream_path: str) -> str | None:
    # /acme/api.git/info/refs -> acme/api ; /acme/api.git/git-receive-pack -> acme/api
    parts = [p for p in upstream_path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return f"{owner}/{name}"


def _service(method: str, upstream_path: str, query: str) -> tuple[str | None, bool]:
    if upstream_path.endswith("/info/refs"):
        svc = (parse_qs(query).get("service") or [None])[0]
        return svc, False
    if upstream_path.endswith("/git-receive-pack"):
        return "git-receive-pack", method.upper() == "POST"
    if upstream_path.endswith("/git-upload-pack"):
        return "git-upload-pack", False
    return None, False


def parse_egress_request(*, method, path, query, headers, body) -> EgressRequest:
    """Map a worker-facing path to its upstream host + repo + smart-HTTP service.

    Fails loud at the boundary: a path outside the known prefixes raises rather
    than defaulting to a host (a mis-forward could leak a credential)."""
    prefix = next((p for p in _PREFIX_HOST if path.startswith(p)), None)
    if prefix is None:
        raise UnmappablePathError(path)
    host = _PREFIX_HOST[prefix]
    upstream_path = path[len(prefix) - 1:]     # keep the leading slash
    repo = _extract_repo(upstream_path) if host == "github.com" else None
    service, is_rp_post = _service(method, upstream_path, query)
    return EgressRequest(
        method=method, upstream_host=host, upstream_path=upstream_path, query=query,
        headers=headers, body=body, repo=repo, service=service,
        is_receive_pack_post=is_rp_post,
    )
